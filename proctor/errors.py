"""Typed exceptions and process exit codes."""

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_INTEGRITY = 4
EXIT_EXTERNAL = 5
EXIT_PERMISSION = 6
EXIT_ABORTED = 7


class ProctorError(Exception):
    """Base error with a stable exit code."""

    exit_code = EXIT_FAILURE

    def __init__(self, message, hint=None):
        super(ProctorError, self).__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self):
        if self.hint:
            return "%s\nHint: %s" % (self.message, self.hint)
        return self.message


class UsageError(ProctorError):
    exit_code = EXIT_USAGE


class ConfigError(ProctorError):
    exit_code = EXIT_USAGE


class EnvironmentError(ProctorError):
    exit_code = EXIT_ENVIRONMENT


class DetectionError(EnvironmentError):
    pass


class IntegrityError(ProctorError):
    exit_code = EXIT_INTEGRITY


class ExternalCommandError(ProctorError):
    exit_code = EXIT_EXTERNAL

    def __init__(self, command, returncode, stderr=None, hint=None):
        if isinstance(command, (list, tuple)):
            command = " ".join(str(part) for part in command)
        detail = (stderr or "").strip()
        message = "command failed (%s): %s" % (returncode, command)
        if detail:
            message += "\n" + detail
        ProctorError.__init__(self, message, hint=hint)
        self.command = command
        self.returncode = returncode
        self.stderr = detail


class PermissionDenied(ProctorError):
    exit_code = EXIT_PERMISSION


class Aborted(ProctorError):
    exit_code = EXIT_ABORTED
