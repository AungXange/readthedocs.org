"""Documentation Builder Environments."""

import structlog
import os
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from docker import APIClient
from docker.errors import APIError as DockerAPIError
from docker.errors import DockerException
from docker.errors import NotFound as DockerNotFoundError
from requests.exceptions import ConnectionError, ReadTimeout
from requests_toolbelt.multipart.encoder import MultipartEncoder
from slumber.exceptions import HttpClientError

from readthedocs.api.v2.client import api as api_v2
from readthedocs.builds.constants import BUILD_STATE_FINISHED
from readthedocs.builds.models import BuildCommandResultMixin
from readthedocs.core.utils import slugify
from readthedocs.projects.exceptions import (
    ProjectConfigurationError,
    RepositoryError,
)
from readthedocs.projects.models import Feature

from .constants import (
    DOCKER_HOSTNAME_MAX_LEN,
    DOCKER_IMAGE,
    DOCKER_LIMITS,
    DOCKER_OOM_EXIT_CODE,
    DOCKER_SOCKET,
    DOCKER_TIMEOUT_EXIT_CODE,
    DOCKER_VERSION,
)
from .exceptions import (
    BuildEnvironmentCreationFailed,
    BuildEnvironmentError,
    BuildEnvironmentException,
    BuildEnvironmentWarning,
    BuildTimeoutError,
    MkDocsYAMLParseError,
    ProjectBuildsSkippedError,
    VersionLockedError,
    YAMLParseError,
)

log = structlog.get_logger(__name__)

__all__ = (
    'api_v2',
    'BuildCommand',
    'DockerBuildCommand',
    'LocalEnvironment',
    'LocalBuildEnvironment',
    'DockerBuildEnvironment',
)


class BuildCommand(BuildCommandResultMixin):

    """
    Wrap command execution for execution in build environments.

    This wraps subprocess commands with some logic to handle exceptions,
    logging, and setting up the env for the build command.

    This acts a mapping of sorts to the API representation of the
    :py:class:`readthedocs.builds.models.BuildCommandResult` model.

    :param command: string or array of command parameters
    :param cwd: Absolute path used as the current working path for the command.
        Defaults to ``RTD_DOCKER_WORKDIR``.
    :param shell: execute command in shell, default=False
    :param environment: environment variables to add to environment
    :type environment: dict
    :param str user: User used to execute the command, it can be in form of ``user:group``
        or ``user``. Defaults to ``RTD_DOCKER_USER``.
    :param build_env: build environment to use to execute commands
    :param bin_path: binary path to add to PATH resolution
    :param description: a more grokable description of the command being run
    :param kwargs: allow to subclass this class and extend it
    """

    def __init__(
            self,
            command,
            cwd=None,
            shell=False,
            environment=None,
            user=None,
            build_env=None,
            bin_path=None,
            description=None,
            record_as_success=False,
            **kwargs,
    ):
        self.command = command
        self.shell = shell
        self.cwd = cwd or settings.RTD_DOCKER_WORKDIR
        self.user = user or settings.RTD_DOCKER_USER
        self._environment = environment.copy() if environment else {}
        if 'PATH' in self._environment:
            raise BuildEnvironmentError('\'PATH\' can\'t be set. Use bin_path')

        self.build_env = build_env
        self.output = None
        self.error = None
        self.start_time = None
        self.end_time = None

        self.bin_path = bin_path
        self.description = description or ''
        self.record_as_success = record_as_success
        self.exit_code = None

    def __str__(self):
        # TODO do we want to expose the full command here?
        output = ''
        if self.output is not None:
            output = self.output.encode('utf-8')
        return '\n'.join([self.get_command(), output])

    def run(self):
        """Set up subprocess and execute command."""
        log.info("Running build command.", command=self.get_command(), cwd=self.cwd)

        self.start_time = datetime.utcnow()
        environment = self._environment.copy()
        if 'DJANGO_SETTINGS_MODULE' in environment:
            del environment['DJANGO_SETTINGS_MODULE']
        if 'PYTHONPATH' in environment:
            del environment['PYTHONPATH']

        # Always copy the PATH from the host into the environment
        env_paths = os.environ.get('PATH', '').split(':')
        if self.bin_path is not None:
            env_paths.insert(0, self.bin_path)
        environment['PATH'] = ':'.join(env_paths)

        try:
            # When using ``shell=True`` the command should be flatten
            command = self.command
            if self.shell:
                command = self.get_command()

            proc = subprocess.Popen(
                command,
                shell=self.shell,
                cwd=self.cwd,
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
            )
            cmd_stdout, cmd_stderr = proc.communicate()
            self.output = self.sanitize_output(cmd_stdout)
            self.error = self.sanitize_output(cmd_stderr)
            self.exit_code = proc.returncode
        except OSError:
            log.exception("Operating system error.")
            self.exit_code = -1
        finally:
            self.end_time = datetime.utcnow()

    def sanitize_output(self, output):
        r"""
        Sanitize ``output`` to be saved into the DB.

            1. Decodes to UTF-8

            2. Replaces NULL (\x00) characters with ``''`` (empty string) to
               avoid PostgreSQL db to fail:
               https://code.djangoproject.com/ticket/28201

            3. Chunk at around ``DATA_UPLOAD_MAX_MEMORY_SIZE`` bytes to be sent
               over the API call request

        :param output: stdout/stderr to be sanitized
        :type output: bytes

        :returns: sanitized output as string or ``None`` if it fails
        """
        try:
            sanitized = output.decode('utf-8', 'replace')
            # Replace NULL (\x00) character to avoid PostgreSQL db to fail
            # https://code.djangoproject.com/ticket/28201
            sanitized = sanitized.replace('\x00', '')
        except (TypeError, AttributeError):
            sanitized = None

        # Chunk the output data to be less than ``DATA_UPLOAD_MAX_MEMORY_SIZE``
        output_length = len(output) if output else 0
        # Left some extra space for the rest of the request data
        threshold = 512 * 1024  # 512Kb
        allowed_length = settings.DATA_UPLOAD_MAX_MEMORY_SIZE - threshold
        if output_length > allowed_length:
            log.info(
                'Command output is too big.',
                project_slug=self.build_env.project.slug,
                version_slug=self.build_env.version.slug,
                build_id=self.build_env.build.get('id'),
                command=self.get_command(),
            )
            truncated_output = sanitized[-allowed_length:]
            sanitized = (
                '.. (truncated) ...\n'
                f'Output is too big. Truncated at {allowed_length} bytes.\n\n\n'
                f'{truncated_output}'
            )

        return sanitized

    def get_command(self):
        """Flatten command."""
        if hasattr(self.command, '__iter__') and not isinstance(self.command, str):
            return ' '.join(self.command)
        return self.command

    def save(self):
        """Save this command and result via the API."""
        # Force record this command as success to avoid Build reporting errors
        # on commands that are just for checking purposes and do not interferes
        # in the Build
        if self.record_as_success:
            log.warning('Recording command exit_code as success')
            self.exit_code = 0

        data = {
            'build': self.build_env.build.get('id'),
            'command': self.get_command(),
            'description': self.description,
            'output': self.output,
            'exit_code': self.exit_code,
            'start_time': self.start_time,
            'end_time': self.end_time,
        }

        if self.build_env.project.has_feature(Feature.API_LARGE_DATA):
            # Don't use slumber directly here. Slumber tries to enforce a string,
            # which will break our multipart encoding here.
            encoder = MultipartEncoder(
                {key: str(value) for key, value in data.items()}
            )
            resource = api_v2.command
            resp = resource._store['session'].post(
                resource._store['base_url'] + '/',
                data=encoder,
                headers={
                    'Content-Type': encoder.content_type,
                }
            )
            log.debug('Post response via multipart form.', response=resp)
        else:
            resp = api_v2.command.post(data)
            log.debug('Post response via JSON encoded data.', response=resp)


class DockerBuildCommand(BuildCommand):

    """
    Create a docker container and run a command inside the container.

    Build command to execute in docker container
    """

    bash_escape_re = re.compile(
        r"([\t\ \!\"\#\$\&\'\(\)\*\:\;\<\>\?\@"
        r'\[\\\]\^\`\{\|\}\~])'
    )

    def __init__(self, *args, escape_command=True, **kwargs):
        """
        Override default to extend behavior.

        :param escape_command: whether escape special chars the command before
            executing it in the container. This should only be disabled on
            trusted or internal commands.
        :type escape_command: bool
        """
        self.escape_command = escape_command
        super().__init__(*args, **kwargs)

    def run(self):
        """Execute command in existing Docker container."""
        log.info(
            "Running build command in container.",
            container_id=self.build_env.container_id,
            command=self.get_command(),
            cwd=self.cwd,
        )

        self.start_time = datetime.utcnow()
        client = self.build_env.get_client()
        try:
            exec_cmd = client.exec_create(
                container=self.build_env.container_id,
                cmd=self.get_wrapped_command(),
                environment=self._environment,
                user=self.user,
                workdir=self.cwd,
                stdout=True,
                stderr=True,
            )

            cmd_output = client.exec_start(exec_id=exec_cmd['Id'], stream=False)
            self.output = self.sanitize_output(cmd_output)
            cmd_ret = client.exec_inspect(exec_id=exec_cmd['Id'])
            self.exit_code = cmd_ret['ExitCode']

            # Docker will exit with a special exit code to signify the command
            # was killed due to memory usage, make the error code
            # nicer. Sometimes the kernel kills the command and Docker doesn't
            # not use the specific exit code, so we check if the word `Killed`
            # is in the last 15 lines of the command's output
            killed_in_output = 'Killed' in '\n'.join(
                self.output.splitlines()[-15:],
            )
            if self.exit_code == DOCKER_OOM_EXIT_CODE or (
                self.exit_code == 1 and
                killed_in_output
            ):
                self.output += str(
                    _(
                        '\n\nCommand killed due to excessive memory consumption\n',
                    ),
                )
        except DockerAPIError:
            self.exit_code = -1
            if self.output is None or not self.output:
                self.output = _('Command exited abnormally')
        finally:
            self.end_time = datetime.utcnow()

    def get_wrapped_command(self):
        """
        Wrap command in a shell and optionally escape special bash characters.

        In order to set the current working path inside a docker container, we
        need to wrap the command in a shell call manually.

        Some characters will be interpreted as shell characters without
        escaping, such as: ``pip install requests<0.8``. When passing
        ``escape_command=True`` in the init method this escapes a good majority
        of those characters.
        """
        prefix = ''
        if self.bin_path:
            bin_path = self._escape_command(self.bin_path)
            prefix += f'PATH={bin_path}:$PATH '

        command = (
            ' '.join(
                self._escape_command(part) if self.escape_command else part
                for part in self.command
            )
        )
        return (
            "/bin/sh -c '{prefix}{cmd}'".format(
                prefix=prefix,
                cmd=command,
            )
        )

    def _escape_command(self, cmd):
        r"""Escape the command by prefixing suspicious chars with `\`."""
        return self.bash_escape_re.sub(r'\\\1', cmd)


class BaseEnvironment:

    """
    Base environment class.

    Used to run arbitrary commands outside a build.
    """

    def __init__(self, project, environment=None):
        # TODO: maybe we can remove this Project dependency also
        self.project = project
        self._environment = environment or {}
        self.commands = []

    def record_command(self, command):
        pass

    def run(self, *cmd, **kwargs):
        """Shortcut to run command from environment."""
        return self.run_command_class(cls=self.command_class, cmd=cmd, **kwargs)

    def run_command_class(
            self, cls, cmd, record=None, warn_only=False,
            record_as_success=False, **kwargs
    ):
        """
        Run command from this environment.

        :param cls: command class to instantiate a command
        :param cmd: command (as a list) to execute in this environment
        :param record: whether or not to record this particular command
            (``False`` implies ``warn_only=True``)
        :param warn_only: don't raise an exception on command failure
        :param record_as_success: force command ``exit_code`` to be saved as
            ``0`` (``True`` implies ``warn_only=True`` and ``record=True``)
        """
        if record is None:
            # ``self.record`` only exists when called from ``*BuildEnvironment``
            record = getattr(self, 'record', False)

        if not record:
            warn_only = True

        if record_as_success:
            record = True
            warn_only = True
            # ``record_as_success`` is needed to instantiate the BuildCommand
            kwargs.update({'record_as_success': record_as_success})

        # Remove PATH from env, and set it to bin_path if it isn't passed in
        environment = self._environment.copy()
        env_path = environment.pop('BIN_PATH', None)
        if 'bin_path' not in kwargs and env_path:
            kwargs['bin_path'] = env_path
        if 'environment' in kwargs:
            raise BuildEnvironmentError('environment can\'t be passed in via commands.')
        kwargs['environment'] = environment

        # ``build_env`` is passed as ``kwargs`` when it's called from a
        # ``*BuildEnvironment``
        build_cmd = cls(cmd, **kwargs)
        build_cmd.run()

        if record:
            # TODO: I don't like how it's handled this entry point here since
            # this class should know nothing about a BuildCommand (which are the
            # only ones that can be saved/recorded)
            self.record_command(build_cmd)

            # We want append this command to the list of commands only if it has
            # to be recorded in the database (to keep consistency) and also, it
            # has to be added after ``self.record_command`` since its
            # ``exit_code`` can be altered because of ``record_as_success``
            self.commands.append(build_cmd)

        if build_cmd.failed:
            msg = 'Command {cmd} failed'.format(cmd=build_cmd.get_command())

            if build_cmd.output:
                msg += ':\n{out}'.format(out=build_cmd.output)

            if warn_only:
                log.warning(
                    msg,
                    project_slug=self.project.slug if self.project else '',
                    version_slug=self.version.slug if self.version else '',
                )
            else:
                raise BuildEnvironmentWarning(msg)
        return build_cmd


class LocalEnvironment(BaseEnvironment):

    # TODO: BuildCommand name doesn't make sense here, should be just Command
    command_class = BuildCommand


class BuildEnvironment(BaseEnvironment):

    """
    Base build environment.

    Base class for wrapping command execution for build steps. This provides a
    context for command execution and reporting, and eventually performs updates
    on the build object itself, reporting success/failure, as well as failures
    during the context manager enter and exit.

    Any exceptions raised inside this context and handled by the eventual
    :py:meth:`__exit__` method, specifically, inside :py:meth:`handle_exception`
    and :py:meth:`update_build`. If the exception is a subclass of
    :py:class:`BuildEnvironmentError`, then this error message is added to the
    build object and is shown to the user as the top-level failure reason for
    why the build failed. Other exceptions raise a general failure warning on
    the build.

    We only update the build through the API in one of three cases:

    * The build is not done and needs an additional build step to follow
    * The build failed and we should always report this change
    * The build was successful and ``update_on_success`` is ``True``

    :param project: Project that is being built
    :param version: Project version that is being built
    :param build: Build instance
    :param record: Record status of build object
    :param environment: shell environment variables
    :param update_on_success: update the build object via API if the build was
                              successful
    """

    # These exceptions are considered ERROR from a Build perspective (the build
    # failed and can continue) but as a WARNING for the application itself (RTD
    # code didn't failed). These exception are logged as ``WARNING`` and they
    # are not sent to Sentry.
    WARNING_EXCEPTIONS = (
        VersionLockedError,
        ProjectBuildsSkippedError,
        YAMLParseError,
        BuildTimeoutError,
        MkDocsYAMLParseError,
        RepositoryError,
        ProjectConfigurationError,
    )

    def __init__(
            self,
            project=None,
            version=None,
            build=None,
            config=None,
            record=True,
            environment=None,
            update_on_success=True,
            start_time=None,
    ):
        super().__init__(project, environment)
        self.version = version
        self.build = build
        self.config = config
        self.record = record
        self.update_on_success = update_on_success

        self.failure = None
        self.start_time = start_time or datetime.utcnow()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        ret = self.handle_exception(exc_type, exc_value, tb)
        self.update_build(BUILD_STATE_FINISHED)
        log.info(
            'Build finished',
            # TODO: move all of these attributes to ``log.bind`` if possible
            project_slug=self.project.slug if self.project else '',
            version_slug=self.version.slug if self.version else '',
            # TODO: add organization_slug here
            success=self.build.get('success') if self.build else '',
            length=self.build.get('length') if self.build else '',
        )
        return ret

    def handle_exception(self, exc_type, exc_value, _):
        """
        Exception handling for __enter__ and __exit__.

        This reports on the exception we're handling and special cases
        subclasses of BuildEnvironmentException. For
        :py:class:`BuildEnvironmentWarning`, exit this context gracefully, but
        don't mark the build as a failure. For all other exception classes,
        including :py:class:`BuildEnvironmentError`, the build will be marked as
        a failure and the context will be gracefully exited.

        If the exception's type is :py:class:`BuildEnvironmentWarning` or it's
        an exception marked as ``WARNING_EXCEPTIONS`` we log the problem as a
        WARNING, otherwise we log it as an ERROR.
        """
        if exc_type is not None:
            log_level_function = None
            if issubclass(exc_type, BuildEnvironmentWarning):
                log_level_function = log.warning
            elif exc_type in self.WARNING_EXCEPTIONS:
                log_level_function = log.warning
                self.failure = exc_value
            else:
                log_level_function = log.error
                self.failure = exc_value

            log_level_function(
                msg=exc_value,
                project_slug=self.project.slug if self.project else '',
                version_slug=self.version.slug if self.version else '',
                exc_info=True,
                extra={
                    'stack': True,
                    'tags': {
                        'build': self.build.get('id') if self.build else '',
                        'project': self.project.slug if self.project else '',
                        'version': self.version.slug if self.version else '',
                    },
                },
            )
            return True

    def record_command(self, command):
        command.save()

    def run(self, *cmd, **kwargs):
        kwargs.update({
            'build_env': self,
        })
        return super().run(*cmd, **kwargs)

    def run_command_class(self, *cmd, **kwargs):  # pylint: disable=arguments-differ
        kwargs.update({
            'build_env': self,
        })
        return super().run_command_class(*cmd, **kwargs)

    @property
    def successful(self):
        """Build completed, without top level failures or failing commands."""
        return (
            self.done and self.failure is None and
            all(cmd.successful for cmd in self.commands)
        )

    @property
    def failed(self):
        """Is build completed, but has top level failure or failing commands."""
        return (
            self.done and (
                self.failure is not None or
                any(cmd.failed for cmd in self.commands)
            )
        )

    @property
    def done(self):
        """Is build in finished state."""
        return (
            self.build and
            self.build['state'] == BUILD_STATE_FINISHED
        )

    def update_build(self, state=None):
        """
        Record a build by hitting the API.

        This step is skipped if we aren't recording the build. To avoid
        recording successful builds yet (for instance, running setup commands
        for the build), set the ``update_on_success`` argument to False on
        environment instantiation.

        If there was an error on the build, update the build regardless of
        whether ``update_on_success`` is ``True`` or not.
        """
        if not self.record:
            return None

        self.build['project'] = self.project.pk
        self.build['version'] = self.version.pk
        self.build['builder'] = socket.gethostname()
        self.build['state'] = state
        if self.done:
            self.build['success'] = self.successful

            # TODO drop exit_code and provide a more meaningful UX for error
            # reporting
            if self.failure and isinstance(
                    self.failure,
                    BuildEnvironmentException,
            ):
                self.build['exit_code'] = self.failure.status_code
            elif self.commands:
                self.build['exit_code'] = max([
                    cmd.exit_code for cmd in self.commands
                ])

        self.build['setup'] = self.build['setup_error'] = ''
        self.build['output'] = self.build['error'] = ''

        if self.start_time:
            build_length = (datetime.utcnow() - self.start_time)
            self.build['length'] = int(build_length.total_seconds())

        if self.failure is not None:
            # Surface a generic error if the class is not a
            # BuildEnvironmentError
            # yapf: disable
            if not isinstance(
                self.failure,
                (
                    BuildEnvironmentException,
                    BuildEnvironmentWarning,
                ),
            ):
                # yapf: enable
                log.error(
                    'Build failed with unhandled exception.',
                    exception=str(self.failure),
                    extra={
                        'stack': True,
                        'tags': {
                            'build': self.build.get('id'),
                            'project': self.project.slug,
                            'version': self.version.slug,
                        },
                    },
                )
                self.failure = BuildEnvironmentError(
                    BuildEnvironmentError.GENERIC_WITH_BUILD_ID.format(
                        build_id=self.build['id'],
                    ),
                )
            self.build['error'] = str(self.failure)

        # Attempt to stop unicode errors on build reporting
        for key, val in list(self.build.items()):
            if isinstance(val, bytes):
                self.build[key] = val.decode('utf-8', 'ignore')

        # We are selective about when we update the build object here
        update_build = (
            # Build isn't done yet, we unconditionally update in this state
            not self.done
            # Build is done, but isn't successful, always update
            or (self.done and not self.successful)
            # Otherwise, are we explicitly to not update?
            or self.update_on_success
        )
        if update_build:
            try:
                api_v2.build(self.build['id']).put(self.build)
            except HttpClientError:
                log.exception(
                    'Unable to update build',
                    build_id=self.build['id'],
                )
            except Exception:
                log.exception('Unknown build exception')


class LocalBuildEnvironment(BuildEnvironment):

    """Local execution build environment."""

    command_class = BuildCommand


class DockerBuildEnvironment(BuildEnvironment):

    """
    Docker build environment, uses docker to contain builds.

    If :py:data:`settings.DOCKER_ENABLE` is true, build documentation inside a
    docker container, instead of the host system, using this build environment
    class.  The build command creates a docker container from a pre-built image,
    defined by :py:data:`settings.DOCKER_IMAGE`.  This container is started with
    a mount to the project's build path under ``user_builds`` on the host
    machine, walling off project builds from reading/writing other projects'
    data.

    :param docker_socket: Override to Docker socket URI
    """

    command_class = DockerBuildCommand
    container_image = DOCKER_IMAGE
    container_mem_limit = DOCKER_LIMITS.get('memory')
    container_time_limit = DOCKER_LIMITS.get('time')

    def __init__(self, *args, **kwargs):
        self.docker_socket = kwargs.pop('docker_socket', DOCKER_SOCKET)
        super().__init__(*args, **kwargs)
        self.client = None
        self.container = None
        self.container_name = self.get_container_name()

        # Decide what Docker image to use, based on priorities:
        # Use the Docker image set by our feature flag: ``testing`` or,
        if self.project.has_feature(Feature.USE_TESTING_BUILD_IMAGE):
            self.container_image = 'readthedocs/build:testing'
        # the image set by user or,
        if self.config and self.config.docker_image:
            self.container_image = self.config.docker_image
        # the image overridden by the project (manually set by an admin).
        if self.project.container_image:
            self.container_image = self.project.container_image

        if self.project.container_mem_limit:
            self.container_mem_limit = self.project.container_mem_limit
        if self.project.container_time_limit:
            self.container_time_limit = self.project.container_time_limit

    def __enter__(self):
        """Start of environment context."""
        try:
            # Test for existing container. We remove any stale containers that
            # are no longer running here if there is a collision. If the
            # container is still running, this would be a failure of the version
            # locking code, so we throw an exception.
            state = self.container_state()
            if state is not None:
                if state.get('Running') is True:
                    exc = BuildEnvironmentError(
                        _(
                            'A build environment is currently '
                            'running for this version',
                        ),
                    )
                    self.failure = exc
                    if self.build:
                        self.build['state'] = BUILD_STATE_FINISHED
                    raise exc

                log.warning(
                    'Removing stale container.',
                    project=self.project.slug,
                    version=self.version.slug,
                    container_id=self.container_id,
                )
                client = self.get_client()
                client.remove_container(self.container_id)
        except (DockerAPIError, ConnectionError):
            # If there is an exception here, we swallow the exception as this
            # was just during a sanity check anyways.
            pass
        except BuildEnvironmentError:
            # There may have been a problem connecting to Docker altogether, or
            # some other handled exception here.
            self.__exit__(*sys.exc_info())
            raise

        # Create the checkout path if it doesn't exist to avoid Docker creation
        if not os.path.exists(self.project.doc_path):
            os.makedirs(self.project.doc_path)

        try:
            self.create_container()
        except:  # noqa
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, exc_type, exc_value, tb):
        """End of environment context."""
        try:
            # Update buildenv state given any container error states first
            self.update_build_from_container_state()

            client = self.get_client()
            try:
                client.kill(self.container_id)
            except DockerNotFoundError:
                log.info(
                    'Container does not exists, nothing to kill.',
                    container_id=self.container_id,
                )
            except DockerAPIError:
                log.exception(
                    'Unable to kill container.',
                    container_id=self.container_id,
                )

            try:
                log.info('Removing container.', container_id=self.container_id)
                client.remove_container(self.container_id)
            except DockerNotFoundError:
                log.info(
                    'Container does not exists, nothing to remove.',
                    container_id=self.container_id,
                )
            # Catch direct failures from Docker API or with an HTTP request.
            # These errors should not surface to the user.
            except (DockerAPIError, ConnectionError, ReadTimeout):
                log.exception(
                    "Couldn't remove container",
                    project=self.project.slug,
                    version=self.version.slug,
                )
            self.container = None
        except BuildEnvironmentError:
            # Several interactions with Docker can result in a top level failure
            # here. We'll catch this and report if there were no reported errors
            # already. These errors are not as important as a failure at deeper
            # code
            if not all([exc_type, exc_value, tb]):
                exc_type, exc_value, tb = sys.exc_info()

        return super().__exit__(exc_type, exc_value, tb)

    def get_container_name(self):
        if self.build:
            name = 'build-{build}-project-{project_id}-{project_name}'.format(
                build=self.build.get('id'),
                project_id=self.project.pk,
                project_name=self.project.slug,
            )
        else:
            # An uuid is added, so the container name is unique per sync.
            uuid_ = uuid.uuid4().hex[:8]
            name = f'sync-{uuid_}-project-{self.project.pk}-{self.project.slug}'
        return slugify(name[:DOCKER_HOSTNAME_MAX_LEN])

    def get_client(self):
        """Create Docker client connection."""
        try:
            if self.client is None:
                self.client = APIClient(
                    base_url=self.docker_socket,
                    version=DOCKER_VERSION,
                )
            return self.client
        except DockerException:
            log.exception(
                "Could not connect to Docker API",
                project_slug=self.project.slug,
                version_slug=self.version.slug,
            )
            # We don't raise an error here mentioning Docker, that is a
            # technical detail that the user can't resolve on their own.
            # Instead, give the user a generic failure
            if self.build:
                error = BuildEnvironmentError.GENERIC_WITH_BUILD_ID.format(
                    build_id=self.build.get('id'),
                )
            else:
                error = 'Failed to connect to Docker API client'
            raise BuildEnvironmentError(error)

    def _get_binds(self):
        """
        Return proper Docker Binds depending on settings.

        It uses Docker Volume if running on a docker-compose. Otherwise, it
        returns just a regular mountpoint path.
        """
        if getattr(settings, 'RTD_DOCKER_COMPOSE', False):
            from pathlib import Path
            binds = {
                settings.RTD_DOCKER_COMPOSE_VOLUME: {
                    'bind': str(Path(self.project.doc_path).parent),
                    'mode': 'rw',
                },
            }
        else:
            binds = {
                self.project.doc_path: {
                    'bind': self.project.doc_path,
                    'mode': 'rw',
                },
            }

        binds.update(settings.RTD_DOCKER_ADDITIONAL_BINDS)

        return binds

    def get_container_host_config(self):
        """
        Create the ``host_config`` settings for the container.

        It mainly generates the proper path bindings between the Docker
        container and the Host by mounting them with the proper permissions.

        The object returned is passed to Docker function
        ``client.create_container``.
        """
        return self.get_client().create_host_config(
            binds=self._get_binds(),
            mem_limit=self.container_mem_limit,
        )

    @property
    def image_hash(self):
        """Return the hash of the Docker image."""
        client = self.get_client()
        image_metadata = client.inspect_image(self.container_image)
        return image_metadata.get('Id')

    @property
    def container_id(self):
        """Return id of container if it is valid."""
        if self.container_name:
            return self.container_name

        if self.container:
            return self.container.get('Id')

    def container_state(self):
        """Get container state."""
        client = self.get_client()
        try:
            info = client.inspect_container(self.container_id)
            return info.get('State', {})
        except DockerAPIError:
            return None

    def update_build_from_container_state(self):
        """
        Update buildenv state from container state.

        In the case of the parent command exiting before the exec commands
        finish and the container is destroyed, or in the case of OOM on the
        container, set a failure state and error message explaining the failure
        on the buildenv.
        """
        state = self.container_state()
        if state is not None and state.get('Running') is False:
            if state.get('ExitCode') == DOCKER_TIMEOUT_EXIT_CODE:
                self.failure = BuildEnvironmentError(
                    _('Build exited due to time out'),
                )
            elif state.get('OOMKilled', False):
                self.failure = BuildEnvironmentError(
                    _('Build exited due to excessive memory consumption'),
                )
            elif state.get('Error'):
                self.failure = BuildEnvironmentError((
                    _('Build exited due to unknown error: {0}').format(
                        state.get('Error'),
                    )
                ),)

    def create_container(self):
        """Create docker container."""
        client = self.get_client()
        try:
            log.info(
                'Creating Docker container.',
                container_image=self.container_image,
                container_id=self.container_id,
            )
            self.container = client.create_container(
                image=self.container_image,
                command=(
                    '/bin/sh -c "sleep {time}; exit {exit}"'.format(
                        time=self.container_time_limit,
                        exit=DOCKER_TIMEOUT_EXIT_CODE,
                    )
                ),
                name=self.container_id,
                hostname=self.container_id,
                host_config=self.get_container_host_config(),
                detach=True,
                user=settings.RTD_DOCKER_USER,
            )
            client.start(container=self.container_id)
        except ConnectionError:
            log.exception(
                'Could not connect to the Docker API, make sure Docker is running.',
                project_slug=self.project.slug,
                version_slug=self.version.slug,
            )
            # We don't raise an error here mentioning Docker, that is a
            # technical detail that the user can't resolve on their own.
            # Instead, give the user a generic failure
            raise BuildEnvironmentError(
                BuildEnvironmentError.GENERIC_WITH_BUILD_ID.format(
                    build_id=self.build['id'],
                ),
            )
        except DockerAPIError as e:
            log.exception(
                e.explanation,
                project_slug=self.project.slug,
                version_slug=self.version.slug,
            )
            raise BuildEnvironmentCreationFailed
