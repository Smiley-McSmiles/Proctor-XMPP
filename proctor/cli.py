"""Proctor command-line interface."""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
from types import SimpleNamespace

from . import __version__
from .analysis import diff_backup, list_users
from .backup import BackupOptions, create_backup
from .config import (
    config_summary, default_config_path, default_repository, initial_config, load_config,
    write_config,
)
from .doctor import run_doctor
from .errors import (
    EXIT_ABORTED, EXIT_FAILURE, EXIT_INTEGRITY, EXIT_OK, EXIT_USAGE, Aborted, ConfigError,
    ProctorError, UsageError,
)
from .hostinfo import HostInfo
from .prosody import detect_prosody
from .provision import (
    ProvisionOptions, detect_webserver, prosody_installed, setup_server,
)
from .reporter import Reporter, table
from .repository import Repository
from .restore import RestoreOptions, migrate_backup, restore_backup
from .schedule import install_schedule, remove_schedule, show_schedule
from .transfer import export_bundle, import_bundle
from .util import (
    human_bytes, isoformat, parse_map, parse_size, shorten,
)

COMPONENTS = ("config", "certs", "data", "plugins", "letsencrypt", "logs", "system",
              "extra", "database")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="FILE", help="proctor config file")
    common.add_argument("--repo", metavar="DIR", help="backup repository directory")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--quiet", action="store_true", help="suppress progress messages")
    common.add_argument("-v", "--verbose", action="count", default=0, help="more detail")
    common.add_argument("--no-color", action="store_true", help="disable colored output")
    common.add_argument("-y", "--yes", action="store_true", help="assume yes for prompts")

    parser = argparse.ArgumentParser(
        prog="proctor",
        description="Back up, restore and migrate a Prosody XMPP server.",
        epilog="Run 'proctor <command> --help' for command options. "
               "Documentation: docs/README.md",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version="proctor %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def command(name, help_text, aliases=None):
        return sub.add_parser(name, help=help_text, aliases=aliases or [],
                              parents=[_suppressed_common()])

    init = command("init", "create the config and backup repository")
    init.add_argument("--path", metavar="FILE", help="config file to write")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.add_argument("--print", dest="print_only", action="store_true",
                      help="print the generated config instead of writing it")
    init.add_argument("--prosody-config", metavar="FILE", help="Prosody config path to record")
    init.add_argument("--setup", action="store_true",
                      help="run the interactive Prosody/web server setup afterwards")
    init.add_argument("--domain", action="append", default=[], metavar="DOMAIN",
                      help="domain for --setup (repeatable)")
    init.add_argument("--webserver", choices=("auto", "none", "nginx", "apache"),
                      default="auto")
    init.add_argument("--certbot", choices=("auto", "yes", "no"), default="auto")
    init.add_argument("--email", metavar="ADDRESS")
    init.add_argument("--firewall", choices=("auto", "yes", "no"), default="auto",
                      help="open XMPP/HTTP ports during --setup when a firewall is active")

    discover = command("discover", "show detected OS, init system and Prosody layout")
    discover.add_argument("--prosody-config", metavar="FILE")

    doctor = command("doctor", "run preflight and health checks")
    doctor.add_argument("--no-config-check", action="store_true",
                        help="skip prosodyctl check config")
    doctor.add_argument("--prosody-config", metavar="FILE")

    turn = command("turn", "check or set up coturn / mod_turn_external",
                   aliases=["coturn"])
    turn.add_argument("--check", action="store_true",
                      help="only report coturn problems (default)")
    turn.add_argument("--setup", action="store_true",
                      help="write coturn + Prosody turn_external configuration")
    turn.add_argument("--domain", action="append", default=[], metavar="DOMAIN",
                      help="XMPP domain (repeatable); defaults to detected VirtualHosts")
    turn.add_argument("--turn-host", metavar="HOST",
                      help="TURN hostname/realm (default turn.<first-domain>)")
    turn.add_argument("--turn-secret", metavar="SECRET")
    turn.add_argument("--turn-external-ip", metavar="IP")
    turn.add_argument("--webserver", choices=("auto", "none", "nginx", "apache"),
                      default="auto")
    turn.add_argument("--certbot", choices=("auto", "yes", "no"), default="auto")
    turn.add_argument("--email", metavar="ADDRESS")
    turn.add_argument("--firewall", choices=("auto", "yes", "no"), default="auto")
    turn.add_argument("--dry-run", action="store_true")
    turn.add_argument("--force", action="store_true",
                      help="overwrite a non-Proctor turnserver.conf without asking")

    setup = command("setup", "install and configure Prosody, nginx/Apache and certbot")
    setup.add_argument("--domain", action="append", default=[], metavar="DOMAIN",
                       help="XMPP domain (repeatable)")
    setup.add_argument("--admin", metavar="JID", help="admin account (default admin@first-domain)")
    setup.add_argument("--webserver", choices=("auto", "none", "nginx", "apache"),
                       default="auto")
    setup.add_argument("--certbot", choices=("auto", "yes", "no"), default="auto")
    setup.add_argument("--email", metavar="ADDRESS", help="Let's Encrypt email")
    setup.add_argument("--firewall", choices=("auto", "yes", "no"), default="auto",
                       help="open XMPP/HTTP ports when UFW or firewalld is active")
    setup.add_argument("--turn", choices=("auto", "yes", "no"), default="auto",
                       help="set up coturn and mod_turn_external for audio/video calls")
    setup.add_argument("--turn-host", metavar="HOST",
                       help="TURN hostname/realm (default turn.<first-domain>)")
    setup.add_argument("--turn-secret", metavar="SECRET",
                       help="shared secret (default: generate a random one)")
    setup.add_argument("--turn-external-ip", metavar="IP",
                       help="public address for coturn behind NAT")
    setup.add_argument("--storage", choices=("internal_hashed", "sqlite", "sql"),
                       default=None,
                       help="storage backend (asks interactively if omitted)")
    setup.add_argument("--proxy-http", dest="proxy_http", action="store_true",
                       default=None, help="proxy HTTP/BOSH/WebSocket through the web server")
    setup.add_argument("--no-proxy-http", dest="proxy_http", action="store_false",
                       default=None, help="do not proxy HTTP endpoints")
    setup.add_argument("--no-start", dest="start", action="store_false", default=True)
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--force", action="store_true",
                       help="overwrite an existing Prosody config without asking")
    setup.add_argument("--prosody-config", metavar="FILE")

    backup = command("backup", "create a backup", aliases=["create"])
    backup.add_argument("--live", action="store_true",
                        help="online backup without stopping Prosody")
    _bool_pair(backup, "--include-logs", "--no-logs", "include_logs",
               "include Prosody logs (default: no)")
    _bool_pair(backup, "--include-system", "--no-system", "include_system",
               "include unit files and /etc/default style files")
    backup.add_argument("--letsencrypt", choices=("auto", "always", "never"), default=None,
                        help="include /etc/letsencrypt (default: auto when referenced)")
    backup.add_argument("--compression", choices=("gz", "xz", "zstd", "none"), default=None)
    _bool_pair(backup, "--encrypt", "--no-encrypt", "encrypt",
               "encrypt with age/GPG using the configured recipients")
    backup.add_argument("--verify", dest="verify_after", action="store_true", default=None,
                        help="verify the archive immediately after writing it")
    backup.add_argument("--skip-database", action="store_true")
    backup.add_argument("--skip-unreadable", action="store_true",
                        help="skip files that disappear/are unreadable (live mode)")
    backup.add_argument("--note", default="", help="note stored in the manifest")
    backup.add_argument("--tag", action="append", default=[], metavar="TAG")
    backup.add_argument("--only", action="append", default=[], metavar="COMPONENT[,..]",
                        help="only these components: %s" % ", ".join(COMPONENTS))
    backup.add_argument("--exclude", action="append", default=[], metavar="COMPONENT[,..]")
    backup.add_argument("--exclude-path", action="append", default=[], metavar="PATH",
                        help="skip this absolute path or directory (for example uploads)")
    backup.add_argument("--include-path", action="append", default=[], metavar="PATH",
                        help="also back up an extra path (proxy config, custom data)")
    backup.add_argument("--prosody-config", metavar="FILE")

    listing = command("list", "list backups", aliases=["ls"])
    listing.add_argument("--limit", type=int, default=0)

    info = command("info", "show a backup manifest")
    info.add_argument("backup", help="backup id, unique prefix, or 'latest'")

    verify = command("verify", "verify archive and member checksums")
    verify.add_argument("backup", help="backup id, unique prefix, or 'latest'")
    verify.add_argument("--quick", action="store_true", help="only verify the outer checksum")

    users = command("users", "list user accounts stored in a backup")
    users.add_argument("backup", help="backup id, unique prefix, or 'latest'")
    users.add_argument("--domain", help="only this XMPP domain")
    users.add_argument("--limit", type=int, default=200)
    users.add_argument("--all", action="store_true", help="show every account")

    restore = command("restore", "restore a backup onto this host")
    _restore_arguments(restore)

    migrate = command("migrate", "migrate a backup onto a new host")
    _restore_arguments(migrate)

    export = command("export", "export a backup as a portable bundle")
    export.add_argument("backup", help="backup id, unique prefix, or 'latest'")
    export.add_argument("-o", "--output", required=True, metavar="FILE")
    export.add_argument("--force", action="store_true", help="overwrite output")

    imp = command("import", "import a bundle or archive into the repository")
    imp.add_argument("file", metavar="FILE")
    imp.add_argument("--manifest", metavar="FILE",
                     help="sidecar manifest for a raw encrypted archive")
    imp.add_argument("--verify", dest="verify_after", action="store_true",
                     help="verify the imported backup")

    prune = command("prune", "apply retention and remove old backups")
    prune.add_argument("--keep-last", type=int, default=None)
    prune.add_argument("--keep-daily", type=int, default=None)
    prune.add_argument("--keep-weekly", type=int, default=None)
    prune.add_argument("--keep-monthly", type=int, default=None)
    prune.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")

    delete = command("delete", "delete one backup", aliases=["rm"])
    delete.add_argument("backup", help="backup id, unique prefix, or 'latest'")
    delete.add_argument("--dry-run", action="store_true")

    diff = command("diff", "compare a backup with this installation")
    diff.add_argument("backup", help="backup id, unique prefix, or 'latest'")

    schedule = command("schedule", "install, remove or show a backup schedule")
    group = schedule.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true")
    group.add_argument("--remove", action="store_true")
    group.add_argument("--show", action="store_true")
    schedule.add_argument("--at", default="03:30", metavar="HH:MM")
    schedule.add_argument("--type", choices=("auto", "systemd", "cron"), default="auto")
    schedule.add_argument("--cron-file", metavar="FILE")
    schedule.add_argument("--command", dest="schedule_command",
                          help="override the proctor command line")
    schedule.add_argument("--live", action="store_true", help="scheduled backups use --live")

    history = command("history", "show backup/restore audit history")
    history.add_argument("--limit", type=int, default=50)

    command("config", "show the effective configuration (secrets redacted)")
    command("version", "show version information")
    return parser


def _suppressed_common():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--repo", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    parser.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS)
    return parser


def _bool_pair(parser, positive, negative, dest, help_text):
    group = parser.add_mutually_exclusive_group()
    group.add_argument(positive, dest=dest, action="store_true", default=None, help=help_text)
    group.add_argument(negative, dest=dest, action="store_false", default=None)


def _restore_arguments(parser):
    parser.add_argument("backup", help="backup id, unique prefix, or 'latest'")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument("--only", action="append", default=[], metavar="COMPONENT[,..]")
    parser.add_argument("--exclude", action="append", default=[], metavar="COMPONENT[,..]")
    parser.add_argument("--keep-config", dest="keep_current_config", action="store_true",
                        help="keep the target's current config files (certs still restored)")
    parser.add_argument("--skip-database", action="store_true")
    parser.add_argument("--no-verify", dest="no_verify", action="store_true",
                        help="skip archive verification (not recommended)")
    parser.add_argument("--no-safety-backup", dest="safety_backup", action="store_false",
                        default=None, help="do not back up the target before overwriting")
    parser.add_argument("--start", dest="start", action="store_true", default=None,
                        help="start Prosody after restoring")
    parser.add_argument("--no-start", dest="start", action="store_false", default=None,
                        help="leave Prosody stopped after restoring")
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                        help="override a source-to-target path mapping")
    parser.add_argument("--no-rewrite-config", dest="rewrite_config", action="store_false",
                        default=True, help="do not rewrite absolute paths in config files")
    parser.add_argument("--no-overwrite", action="store_true",
                        help="skip files that already exist")
    parser.add_argument("--force", action="store_true",
                        help="allow non-interactive operation on unusual targets")
    parser.add_argument("--prosody-config", metavar="FILE",
                        help="target Prosody config path")
    _bool_pair(parser, "--install-missing", "--no-install-missing", "install_missing",
               "install missing Prosody/web packages when confirmed")
    parser.add_argument("--webserver", choices=("auto", "none", "nginx", "apache"),
                        default="auto", help="web server for HTTP/BOSH/WebSocket")
    parser.add_argument("--certbot", choices=("auto", "yes", "no"), default="auto")
    parser.add_argument("--email", metavar="ADDRESS", help="Let's Encrypt email")
    parser.add_argument("--firewall", choices=("auto", "yes", "no"), default="auto",
                        help="open XMPP/HTTP ports when UFW or firewalld is active")
    parser.add_argument("--domain", action="append", default=[], metavar="DOMAIN",
                        help="override web/TLS domains during provisioning (repeatable)")
    parser.add_argument("--admin", metavar="JID",
                        help="admin JID (used when provisioning a fresh server)")
    _bool_pair(parser, "--proxy-http", "--no-proxy-http", "proxy_http",
               "proxy HTTP endpoints through the web server")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    json_mode = bool(getattr(args, "json", False))
    color = (not json_mode and not bool(getattr(args, "no_color", False))
             and os.environ.get("NO_COLOR") is None)
    reporter = Reporter(json_mode=json_mode, quiet=bool(getattr(args, "quiet", False)),
                        verbose=int(getattr(args, "verbose", 0) or 0), color=color)
    try:
        return _dispatch(args, reporter)
    except KeyboardInterrupt:
        reporter.error("interrupted")
        return EXIT_ABORTED
    except Aborted as exc:
        reporter.error(str(exc))
        return exc.exit_code
    except ProctorError as exc:
        reporter.error(str(exc))
        if json_mode:
            reporter.emit({"error": exc.message, "hint": exc.hint,
                           "exit_code": exc.exit_code})
        return exc.exit_code
    except PermissionError as exc:
        reporter.error("permission denied: %s" % exc)
        if json_mode:
            reporter.emit({"error": "permission denied", "detail": str(exc),
                           "exit_code": 6})
        return 6
    except OSError as exc:
        reporter.error(str(exc))
        if json_mode:
            reporter.emit({"error": str(exc), "exit_code": EXIT_FAILURE})
        return EXIT_FAILURE


def _dispatch(args, reporter):
    command = args.command
    if command == "version":
        return _cmd_version(reporter)
    if command == "init":
        return _cmd_init(args, reporter)
    if command == "discover":
        return _cmd_discover(args, reporter)

    config_path = getattr(args, "config", None)
    config = load_config(config_path, allow_missing=not bool(config_path))
    if config_path and not config.path:
        raise ConfigError("config file not found: %s" % config_path)
    if getattr(args, "prosody_config", None):
        config.prosody["config"] = os.path.abspath(os.path.expanduser(args.prosody_config))
    repository = Repository(getattr(args, "repo", None) or config.repository)

    if command == "setup":
        return _cmd_setup(args, config, reporter)
    if command == "doctor":
        if getattr(args, "prosody_config", None):
            config.prosody["config"] = os.path.abspath(
                os.path.expanduser(args.prosody_config)
            )
        result = run_doctor(config, repository, reporter,
                            check_config=not args.no_config_check)
        reporter.emit(result, human=_fmt_doctor)
        return EXIT_OK if result["ok"] else EXIT_FAILURE
    if command in ("turn", "coturn"):
        return _cmd_turn(args, config, reporter)
    if command in ("backup", "create"):
        return _cmd_backup(args, config, repository, reporter)
    if command in ("list", "ls"):
        return _cmd_list(args, repository, reporter)
    if command == "info":
        return _cmd_info(args, repository, config, reporter)
    if command == "verify":
        return _cmd_verify(args, repository, config, reporter)
    if command == "users":
        return _cmd_users(args, repository, config, reporter)
    if command == "restore":
        return _cmd_restore(args, config, repository, reporter, mode="restore")
    if command == "migrate":
        return _cmd_restore(args, config, repository, reporter, mode="migrate")
    if command == "export":
        return _cmd_export(args, repository, reporter)
    if command == "import":
        return _cmd_import(args, repository, config, reporter)
    if command == "prune":
        return _cmd_prune(args, config, repository, reporter)
    if command in ("delete", "rm"):
        return _cmd_delete(args, repository, reporter)
    if command == "diff":
        return _cmd_diff(args, config, repository, reporter)
    if command == "schedule":
        return _cmd_schedule(args, config, reporter)
    if command == "history":
        return _cmd_history(args, repository, reporter)
    if command == "config":
        result = config_summary(config)
        result["redacted"] = config.redacted()
        reporter.emit(result, human=lambda value: json.dumps(value["redacted"], indent=2))
        return EXIT_OK
    raise UsageError("unknown command: %s" % command)


# -- command implementations ---------------------------------------------

def _cmd_version(reporter):
    if reporter.json_mode:
        reporter.emit({
            "name": "proctor",
            "version": __version__,
            "python": sys.version.split()[0],
            "format_version": 1,
        })
    else:
        reporter.emit("proctor %s (Python %s)" % (__version__, sys.version.split()[0]))
    return EXIT_OK


def _cmd_init(args, reporter):
    host = HostInfo()
    if args.print_only:
        data = initial_config(host, repository=args.repo)
        if args.prosody_config:
            data["prosody"]["config"] = os.path.abspath(os.path.expanduser(args.prosody_config))
        reporter.emit(data, human=lambda value: json.dumps(value, indent=2))
        return EXIT_OK
    path = os.path.abspath(os.path.expanduser(args.path or default_config_path(host)))
    if os.path.exists(path) and not args.force:
        raise ConfigError("config already exists: %s" % path,
                          hint="use --force to overwrite or --path to choose another file")
    data = initial_config(host, repository=args.repo)
    data["repository"] = os.path.abspath(os.path.expanduser(data["repository"]))
    if args.prosody_config:
        data["prosody"]["config"] = os.path.abspath(os.path.expanduser(args.prosody_config))
    else:
        try:
            install = detect_prosody(host, require=False)
            if install is not None:
                data["prosody"]["config"] = install.config_file
        except ProctorError:
            pass
    write_config(path, data)
    repository = Repository(data["repository"])
    with repository.lock():
        repository.ensure()
    result = {
        "config": path,
        "repository": repository.path,
        "host": host.as_dict(),
    }
    if args.setup:
        initialized = load_config(path)
        setup_options = ProvisionOptions(
            install_missing=None,
            webserver=args.webserver,
            certbot=args.certbot,
            email=args.email,
            firewall=args.firewall,
            domains=args.domain,
            assume_yes=args.yes,
        )
        result["setup"] = setup_server(initialized, setup_options, reporter)
    reporter.emit(result, human=lambda value: (
        "Wrote %s\nInitialized repository %s\n\nNext: proctor doctor"
        % (value["config"], value["repository"])
        + ("\n\n" + _fmt_setup(value["setup"]) if value.get("setup") else "")
    ))
    return EXIT_OK


def _cmd_discover(args, reporter):
    host = HostInfo()
    config_path = getattr(args, "config", None)
    config = load_config(config_path, allow_missing=not bool(config_path))
    if getattr(args, "prosody_config", None):
        config.prosody["config"] = os.path.abspath(
            os.path.expanduser(args.prosody_config)
        )
    try:
        install = detect_prosody(host, overrides={
            "config": config.prosody.get("config"),
            "data_dir": config.prosody.get("data_dir"),
            "log_dir": config.prosody.get("log_dir"),
            "cert_dirs": config.prosody.get("cert_dirs"),
            "plugin_dirs": config.prosody.get("plugin_dirs"),
            "user": config.prosody.get("user"),
            "group": config.prosody.get("group"),
        }, require=False)
    except ProctorError as exc:
        install = None
        reporter.warn(str(exc))
    try:
        web = detect_webserver(host)
    except ProctorError:
        web = None
    result = {
        "host": host.as_dict(),
        "prosody_installed": prosody_installed(host),
        "prosody": install.as_dict() if install else None,
        "webserver": web.as_dict() if web is not None else None,
        "repository": getattr(args, "repo", None) or config.repository,
    }
    reporter.emit(result, human=_fmt_discover)
    return EXIT_OK


def _cmd_setup(args, config, reporter):
    if getattr(args, "prosody_config", None):
        config.prosody["config"] = os.path.abspath(
            os.path.expanduser(args.prosody_config)
        )
    options = ProvisionOptions(
        install_missing=None,
        webserver=args.webserver,
        certbot=args.certbot,
        email=args.email,
        domains=args.domain,
        admin=args.admin,
        proxy_http=args.proxy_http,
        storage=args.storage,
        start=args.start,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        force=args.force,
        firewall=args.firewall,
        turn=args.turn,
        turn_host=args.turn_host,
        turn_secret=args.turn_secret,
        turn_external_ip=args.turn_external_ip,
    )
    result = setup_server(config, options, reporter)
    reporter.emit(result, human=_fmt_setup)
    return EXIT_FAILURE if result.get("certbot_failed") else EXIT_OK


def _cmd_turn(args, config, reporter):
    from .turn import (
        detect_coturn, patch_prosody_config, prosody_turn_settings,
        setup_turn, turn_checks, turn_realm, validate_turnserver_conf,
    )
    from .provision import (
        ProvisionOptions, Prompter, detect_prosody, detect_webserver,
        normalize_domains,
    )
    domains = normalize_domains(args.domain)
    install = None
    try:
        install = detect_prosody(config.host, overrides={
            "config": config.prosody.get("config"),
            "data_dir": config.prosody.get("data_dir"),
        }, require=False)
    except ProctorError:
        install = None
    if not domains and install is not None:
        domains = list(install.virtualhosts or [])
    if args.setup:
        if not domains:
            raise ConfigError("--domain is required when Prosody is not configured yet")
        realm = turn_realm(domains, args.turn_host)
        options = ProvisionOptions(
            install_missing=None,
            webserver=args.webserver,
            certbot=args.certbot,
            email=args.email,
            domains=domains,
            turn="yes",
            turn_host=args.turn_host,
            turn_secret=args.turn_secret,
            turn_external_ip=args.turn_external_ip,
            firewall=args.firewall,
            dry_run=args.dry_run,
            force=args.force,
        )
        prompter = Prompter(assume_yes=bool(args.yes))
        web = None
        if (args.webserver or "auto") != "none":
            web = detect_webserver(config.host) if (args.webserver or "auto") == "auto" \
                else SimpleNamespace(kind=args.webserver)
        cert_name = domains[0] if which_certificate(domains[0]) else None
        result = setup_turn(
            config, options, reporter, prompter, domains, web,
            bool(args.certbot == "yes"),
            email=args.email, cert_name=cert_name, realm=realm,
        )
        if result.get("config") and not args.dry_run and result.get("installed", {}).get("installed"):
            result["config_path"] = "/etc/coturn/turnserver.conf"
        if result.get("secret"):
            settings = prosody_turn_settings(domains[0], result["secret"])
            config_path = config.prosody.get("config")
            if config_path:
                if args.dry_run:
                    preview = patch_prosody_config(config_path, settings, dry_run=True)
                    result["prosody_config_preview"] = preview
                else:
                    patch_prosody_config(config_path, settings)
        reporter.emit(result, human=_fmt_turn_setup)
        return EXIT_FAILURE if result.get("validated") is False else EXIT_OK
    turn = detect_coturn()
    checks = turn_checks(config, install)
    result = {"turn": turn.as_dict(), "checks": checks,
              "ok": not any(status == "fail" for _name, status, _detail in checks)}
    reporter.emit(result, human=_fmt_turn_checks)
    return EXIT_OK if result["ok"] else EXIT_FAILURE


def which_certificate(name):
    from .provision import has_certificate
    try:
        return has_certificate(name)
    except Exception:
        return False


def _fmt_turn_checks(value):
    rows = [[status.upper(), name, detail]
            for name, status, detail in value["checks"]]
    if not rows:
        return "coturn is not installed and /etc/coturn/turnserver.conf is missing."
    return "%s\n\nRun 'sudo proctor turn --setup' to generate a working config." % table(
        ["STATUS", "CHECK", "DETAIL"], rows)


def _fmt_turn_setup(value):
    web = value.get("web") or {}
    lines = [
        "coturn setup%s" % (" (dry run, no changes)" if value.get("dry_run") else ""),
        "Realm:      %s" % (value.get("realm") or "-"),
        "Secret:     %s" % ("set (see /etc/coturn/turnserver.conf)" if value.get("secret") else "-"),
        "Config:     %s" % (value.get("config_path") or "-"),
        "Vhost:      %s" % (web.get("path") or "-"),
        "Firewall:   %s" % _firewall_text(value.get("firewall") or {}),
        "SELinux:    %s" % ((value.get("selinux") or {}).get("status") or "-"),
        "Service:    %s" % ((value.get("service") or {}).get("status") or "-"),
    ]
    firewall = value.get("firewall") or {}
    if firewall.get("relay_range"):
        lines.append("Relay UDP:  %s" % firewall["relay_range"])
    if value.get("validated") is False:
        lines.append("WARNING: coturn rejected the generated configuration")
    if value.get("config"):
        redacted = re.sub(r"(?m)^(static-auth-secret=).*$", r"\1<redacted>", value["config"])
        lines.extend(["", "# turnserver.conf", redacted])
    if web.get("text"):
        lines.extend(["", "# %s vhost" % (web.get("kind") or "web"), web["text"]])
    if value.get("prosody_config_preview"):
        lines.extend(["", "# Prosody preview", value["prosody_config_preview"]])
    return "\n".join(lines)


def _cmd_backup(args, config, repository, reporter):
    only = parse_components(args.only)
    exclude = parse_components(args.exclude)
    if "database" in exclude:
        args.skip_database = True
    if "certs" in exclude:
        exclude.add("letsencrypt")
    if "certs" in only:
        # Certbot's live/archive tree is the certificate store on the hosts
        # Proctor supports; `--only certs` should include it.
        only.add("letsencrypt")
    if only and "database" not in only and "data" not in only:
        args.skip_database = True
    include_logs = args.include_logs
    if include_logs is None:
        include_logs = bool(config.backup.get("include_logs", False))
    if "logs" in only:
        include_logs = True
    include_system = args.include_system
    if include_system is None:
        include_system = bool(config.backup.get("include_system", True))
    if "system" in only:
        include_system = True
    live = args.live or not bool(config.backup.get("stop_service", True))
    options = BackupOptions(
        live=live,
        include_logs=include_logs,
        include_system=include_system,
        include_letsencrypt=(args.letsencrypt or
                             config.backup.get("include_letsencrypt", "auto")),
        compression=(args.compression or config.backup.get("compression", "gz")),
        encrypt=args.encrypt,
        verify_after=(args.verify_after if args.verify_after is not None
                      else bool(config.backup.get("verify_after", False))),
        skip_database=args.skip_database,
        skip_unreadable=args.skip_unreadable,
        note=args.note,
        tags=args.tag,
        only=only,
        exclude=exclude,
        exclude_paths=args.exclude_path,
        include_paths=args.include_path,
    )
    result = create_backup(config, repository, options, reporter)
    entry = result["entry"]
    summary = {
        "id": entry["id"],
        "archive": entry["archive"],
        "path": result["archive_path"],
        "size": entry["size"],
        "sha256": entry["sha256"],
        "encrypted": entry.get("encrypted"),
        "compression": entry.get("compression"),
        "components": entry.get("components"),
        "database": entry.get("database"),
        "users": entry.get("users"),
        "tags": entry.get("tags"),
        "note": entry.get("note"),
        "warnings": result["warnings"],
        "skipped": result["skipped"],
        "repository": result["repository"],
    }
    reporter.emit(summary, human=_fmt_backup)
    return EXIT_OK


def _cmd_list(args, repository, reporter):
    if not repository.exists() and os.path.isdir(repository.backups_dir):
        repository.init()
    if not repository.exists():
        result = {"repository": repository.path, "count": 0, "backups": []}
    else:
        entries = repository.list_entries()
        if args.limit and args.limit > 0:
            entries = entries[: args.limit]
        result = {
            "repository": repository.path,
            "count": len(entries),
            "backups": entries,
        }
    reporter.emit(result, human=_fmt_list)
    return EXIT_OK


def _cmd_info(args, repository, config, reporter):
    entry = repository.get(args.backup)
    manifest = repository.manifest(entry)
    result = {"entry": entry, "manifest": manifest}
    reporter.emit(result, human=lambda value: _fmt_info(value["entry"], value["manifest"]))
    return EXIT_OK


def _cmd_verify(args, repository, config, reporter):
    entry = repository.get(args.backup)
    result = repository.verify(entry, encryption=config.encryption, reporter=reporter,
                               quick=args.quick)
    reporter.emit(result, human=_fmt_verify)
    return EXIT_INTEGRITY if result["errors"] else EXIT_OK


def _cmd_users(args, repository, config, reporter):
    result = list_users(repository, args.backup, encryption=config.encryption,
                        reporter=reporter, domain=args.domain)
    if not args.all and args.limit > 0:
        result["truncated"] = result["count"] > args.limit
        result["users"] = result["users"][: args.limit]
    reporter.emit(result, human=_fmt_users)
    return EXIT_OK


def _cmd_restore(args, config, repository, reporter, mode):
    if args.prosody_config:
        config.prosody["config"] = os.path.abspath(os.path.expanduser(args.prosody_config))
    only = parse_components(args.only)
    exclude = parse_components(args.exclude)
    maps = parse_map(args.map)
    entry = repository.get(args.backup)
    if not args.dry_run:
        _confirm(
            "Restore backup %s from %s onto this host? Existing Prosody files may be "
            "overwritten." % (entry["id"], entry.get("hostname") or "unknown"),
            args, reporter,
        )
    options = RestoreOptions(
        dry_run=args.dry_run,
        verify=not args.no_verify,
        only=only,
        exclude=exclude,
        keep_current_config=args.keep_current_config,
        skip_database=args.skip_database,
        start=args.start,
        safety_backup=args.safety_backup,
        maps=maps,
        rewrite_config=args.rewrite_config,
        no_overwrite=args.no_overwrite,
        force=args.force,
        mode=mode,
        install_missing=args.install_missing,
        webserver=args.webserver,
        certbot=args.certbot,
        firewall=args.firewall,
        email=args.email,
        domains=args.domain,
        admin=args.admin,
        proxy_http=args.proxy_http,
        assume_yes=args.yes,
    )
    result = restore_backup(config, repository, entry["id"], options, reporter)
    reporter.emit(result, human=_fmt_restore)
    service = result.get("service") or {}
    provisioning = result.get("provisioning") or {}
    should_start = (args.start is True
                    or (args.start is None
                        and (bool(service.get("was_running"))
                             or bool(provisioning.get("prosody_new")))))
    web = result.get("webserver") or {}
    failed = (service.get("validated") is False
              or (should_start and not service.get("started"))
              or (web.get("certificate_requested") and not web.get("tls"))
              or bool(web.get("certificate_error")))
    return EXIT_FAILURE if failed else EXIT_OK


def _cmd_export(args, repository, reporter):
    result = export_bundle(repository, args.backup, args.output, force=args.force,
                           reporter=reporter)
    reporter.emit(result, human=lambda value: (
        "Exported %s\nBundle: %s (%s)\nSHA-256: %s"
        % (value["id"], value["output"], human_bytes(value["size"]), value["sha256"])
    ))
    return EXIT_OK


def _cmd_import(args, repository, config, reporter):
    with repository.lock():
        result = import_bundle(repository, args.file, manifest_path=args.manifest,
                               reporter=reporter)
        repository.append_history("import", {
            "id": result["entry"]["id"],
            "source": os.path.abspath(args.file),
            "new": result["imported"],
        }, limit=config.data.get("history_limit", 5000))
    if args.verify_after:
        verify = repository.verify(result["entry"], encryption=config.encryption,
                                   reporter=reporter)
        result["verify"] = verify
        if verify["errors"]:
            reporter.emit(result, human=_fmt_import)
            return EXIT_INTEGRITY
    reporter.emit(result, human=_fmt_import)
    return EXIT_OK


def _cmd_prune(args, config, repository, reporter):
    if not repository.exists():
        raise ConfigError("repository does not exist: %s" % repository.path)
    policy = dict(config.retention)
    for key in ("keep_last", "keep_daily", "keep_weekly", "keep_monthly"):
        value = getattr(args, key)
        if value is not None:
            policy[key] = value
    if args.apply:
        _confirm("Delete backups that fall outside the retention policy?", args, reporter)
    with repository.lock():
        result = repository.prune(policy, dry_run=not args.apply)
        if args.apply:
            repository.append_history("prune", {
                "removed": [item["id"] for item in result["removed"]],
                "freed": result["freed"],
            }, limit=config.data.get("history_limit", 5000))
    reporter.emit(result, human=_fmt_prune)
    return EXIT_OK


def _cmd_delete(args, repository, reporter):
    entry = repository.get(args.backup)
    if not args.dry_run:
        _confirm("Delete backup %s permanently?" % entry["id"], args, reporter)
    with repository.lock():
        result = repository.delete(entry, dry_run=args.dry_run)
        if not args.dry_run:
            repository.append_history("delete", {"id": entry["id"],
                                                 "freed": result["freed"]})
    reporter.emit(result, human=lambda value: (
        "%s %s\nFreed: %s" % ("Would delete" if value["dry_run"] else "Deleted",
                              value["id"], human_bytes(value["freed"]))
    ))
    return EXIT_OK


def _cmd_diff(args, config, repository, reporter):
    result = diff_backup(config, repository, args.backup, encryption=config.encryption,
                         reporter=reporter)
    reporter.emit(result, human=_fmt_diff)
    return EXIT_OK


def _cmd_schedule(args, config, reporter):
    if args.show:
        result = show_schedule(config, cron_file=args.cron_file)
        reporter.emit(result, human=_fmt_schedule_show)
        return EXIT_OK
    if args.remove:
        result = remove_schedule(config, cron_file=args.cron_file, reporter=reporter)
        reporter.emit(result, human=lambda value: (
            "Removed: %s" % ", ".join(value["removed"])
            if value["removed"] else "No Proctor schedule found"
        ))
        return EXIT_OK
    result = install_schedule(config, at=args.at, kind=args.type,
                              cron_file=args.cron_file, live=args.live,
                              command=args.schedule_command, reporter=reporter)
    reporter.emit(result, human=lambda value: (
        "Installed %s schedule at %s\n%s" % (
            value["kind"], value["at"],
            value.get("timer") or value.get("cron_file") or "")
    ))
    return EXIT_OK


def _cmd_history(args, repository, reporter):
    result = {"repository": repository.path,
              "history": repository.history(limit=args.limit)
              if os.path.isdir(repository.path) else []}
    reporter.emit(result, human=_fmt_history)
    return EXIT_OK


# -- helpers -------------------------------------------------------------

def parse_components(values):
    result = set()
    for value in values or []:
        for item in str(value).split(","):
            item = item.strip().lower()
            if not item:
                continue
            if item not in COMPONENTS:
                raise UsageError("unknown component %r (choose from %s)"
                                 % (item, ", ".join(COMPONENTS)))
            result.add(item)
    return result


def _confirm(message, args, reporter):
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise Aborted("confirmation required; pass --yes for non-interactive use")
    reporter.info(message)
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        raise Aborted("aborted by user")


# -- human formatters ----------------------------------------------------

def _fmt_backup(value):
    lines = [
        "Backup created: %s" % value["id"],
        "Archive:        %s (%s)" % (value["archive"], human_bytes(value["size"])),
        "SHA-256:        %s" % value["sha256"],
        "Encrypted:      %s" % ("yes" if value["encrypted"] else "no"),
        "Components:     %s" % _component_summary(value.get("components") or {}),
        "Database:       %s" % (value.get("database") or "none"),
        "User accounts:  %s" % (value.get("users") if value.get("users") is not None else "?"),
        "Repository:     %s" % value["repository"],
    ]
    if value.get("tags"):
        lines.append("Tags:           %s" % ", ".join(value["tags"]))
    if value.get("note"):
        lines.append("Note:           %s" % value["note"])
    if value.get("warnings"):
        lines.append("Warnings:")
        lines.extend("  - %s" % item for item in value["warnings"])
    if value.get("skipped"):
        lines.append("Skipped files:  %d" % len(value["skipped"]))
    return "\n".join(lines)


def _component_summary(components):
    if not components:
        return "none"
    parts = []
    for name in sorted(components):
        stats = components[name]
        parts.append("%s (%d files, %s)" % (
            name, stats.get("files", 0), human_bytes(stats.get("bytes", 0))))
    return ", ".join(parts)


def _fmt_list(value):
    if not value["backups"]:
        return "No backups in %s" % value["repository"]
    rows = []
    for entry in value["backups"]:
        rows.append([
            entry.get("id", ""),
            (entry.get("created_at") or "").replace("T", " ").replace("Z", ""),
            entry.get("hostname") or "",
            human_bytes(entry.get("size") or 0),
            "yes" if entry.get("encrypted") else "no",
            str(entry.get("users") if entry.get("users") is not None else "-"),
            shorten(entry.get("note") or "", 30),
        ])
    return "Repository: %s\n\n%s" % (
        value["repository"],
        table(["ID", "CREATED (UTC)", "SOURCE", "SIZE", "ENC", "USERS", "NOTE"], rows),
    )


def _fmt_info(entry, manifest):
    source = manifest.get("source", {})
    prosody = source.get("prosody", {}) or {}
    stats = manifest.get("stats", {})
    db = manifest.get("database") or {}
    lines = [
        "ID:            %s" % entry["id"],
        "Created:       %s" % manifest.get("created_at"),
        "Proctor:       %s (format %s)" % (manifest.get("proctor_version"),
                                            manifest.get("format_version")),
        "Source host:   %s (%s)" % (source.get("hostname"), source.get("os", {}).get("pretty_name")),
        "Prosody:       %s" % (source.get("prosody_version") or "unknown"),
        "Archive:       %s (%s)" % (entry.get("archive"), human_bytes(entry.get("size") or 0)),
        "SHA-256:       %s" % entry.get("sha256"),
        "Encrypted:     %s" % ("yes" if entry.get("encrypted") else "no"),
        "Compression:   %s" % entry.get("compression"),
        "Components:    %s" % _component_summary(manifest.get("components") or {}),
        "Database:      %s" % (db.get("engine") or "none"),
        "Users:         %s" % stats.get("user_count"),
        "Files:         %s" % stats.get("entries"),
        "Config:        %s" % prosody.get("config_file"),
        "Data:          %s" % prosody.get("data_dir"),
        "Logs:          %s" % prosody.get("log_dir"),
        "Storage:       %s" % prosody.get("storage"),
        "VirtualHosts:  %s" % (", ".join(prosody.get("virtualhosts") or []) or "-"),
        "Tags:          %s" % (", ".join(manifest.get("tags") or []) or "-"),
        "Note:          %s" % (manifest.get("note") or "-"),
    ]
    if manifest.get("warnings"):
        lines.append("Warnings:")
        lines.extend("  - %s" % item for item in manifest["warnings"])
    return "\n".join(lines)


def _fmt_verify(value):
    lines = [
        "Backup:        %s" % value["id"],
        "Archive:       %s" % value["archive"],
        "Encrypted:     %s" % ("yes" if value["encrypted"] else "no"),
        "SHA-256:       %s" % ("OK" if value["checksum_ok"] else "MISMATCH"),
        "Manifest:      %s" % ("OK" if value["manifest_ok"] else
                                ("not checked" if value["members_ok"] is None else "MISMATCH")),
        "Members:       %s" % ("OK" if value["members_ok"] else
                                ("not checked" if value["members_ok"] is None else "FAILED")),
    ]
    if value["errors"]:
        lines.append("Errors:")
        lines.extend("  - %s" % item for item in value["errors"])
    if value["warnings"]:
        lines.append("Warnings:")
        lines.extend("  - %s" % item for item in value["warnings"])
    return "\n".join(lines)


def _fmt_users(value):
    lines = ["Backup: %s" % value["id"], "Accounts: %d" % value["count"]]
    if value.get("domains"):
        lines.append("Domains:  %s" % ", ".join(
            "%s (%d)" % (domain, count) for domain, count in sorted(value["domains"].items())))
    if value.get("users"):
        lines.append("")
        lines.extend(value["users"])
    if value.get("truncated"):
        lines.append("... output truncated (use --all)")
    return "\n".join(lines)


def _fmt_restore(value):
    prefix = "Dry run" if value.get("dry_run") else "Done"
    counts = value.get("extracted") or {}
    lines = [
        "%s: %s (%s)" % (prefix, value["id"], value["mode"]),
        "Source:        %s (%s)" % (value.get("source_hostname"), value.get("source_os")),
        "Target:        %s (%s)" % (value.get("target_hostname"), value.get("target_os")),
        "Files:         %s (%s)" % (counts.get("files", 0), human_bytes(counts.get("bytes", 0))),
        "Directories:   %s" % counts.get("dirs", 0),
        "Symlinks:      %s" % counts.get("symlinks", 0),
        "Skipped:       %s" % counts.get("skipped", 0),
        "Database:      %s" % (
            "restored" if (value.get("database") or {}).get("result") else
            ("skipped" if not (value.get("database") or {}).get("restore") else "not restored")),
    ]
    if value.get("safety_backup"):
        lines.append("Safety backup: %s" % value["safety_backup"])
    service = value.get("service") or {}
    lines.append("Service:       %s (was running: %s, started: %s)" % (
        service.get("name"), service.get("was_running"), service.get("started")))
    provisioning = value.get("provisioning") or {}
    if provisioning.get("plan"):
        plan = provisioning["plan"]
        lines.append(
            "Provisioning:   would install Prosody: %s; web server: %s; certbot: %s"
            % ("yes" if plan.get("install_prosody") else "no",
               plan.get("webserver") or "none", plan.get("certbot") or "auto"))
    elif provisioning:
        lines.append("Provisioning:   web server: %s; certbot: %s; skipped: %s" % (
            (provisioning.get("web") or {}).get("kind") or "none",
            "yes" if provisioning.get("certbot") else "no",
            "yes" if provisioning.get("skipped") else "no"))
    web = value.get("webserver") or {}
    if web.get("kind"):
        lines.append("Web server:     %s (TLS: %s, config: %s)" % (
            web.get("kind"), "yes" if web.get("tls") else "no",
            web.get("config") or "-"))
        if web.get("certificate_error"):
            lines.append("TLS warning:    %s" % web["certificate_error"])
        elif web.get("certificate_requested") and not web.get("tls"):
            lines.append("TLS warning:    certbot did not produce a certificate")
    if web.get("firewall"):
        lines.append("Firewall:       %s" % _firewall_text(web["firewall"]))
    turn = value.get("turn") or {}
    if provisioning.get("turn"):
        lines.append("TURN:           %s (realm %s)" % (
            "configured" if value.get("turn") else "requested",
            provisioning.get("turn_realm") or "-"))
    elif turn.get("enabled"):
        lines.append("TURN:           %s (realm %s)" % (
            "configured", turn.get("realm") or "-"))
    if value.get("rewritten_configs"):
        lines.append("Rewritten configs:")
        lines.extend("  - %s" % item for item in value["rewritten_configs"])
    if value.get("path_map"):
        lines.append("Path map:")
        for item in value["path_map"]:
            if item["from"] != item["to"]:
                lines.append("  %s -> %s" % (item["from"], item["to"]))
    if value.get("warnings"):
        lines.append("Warnings:")
        lines.extend("  - %s" % item for item in value["warnings"])
    if value.get("dry_run") and value.get("actions"):
        lines.append("Planned actions (first %d):" % len(value["actions"]))
        for item in value["actions"][:40]:
            lines.append("  %-8s %s" % (item["type"], item["destination"]))
        if len(value["actions"]) > 40:
            lines.append("  ...")
    if value.get("checklist"):
        lines.append("Migration checklist:")
        lines.extend("  [ ] %s" % item for item in value["checklist"])
    return "\n".join(lines)


def _fmt_doctor(value):
    rows = []
    for check in value["checks"]:
        rows.append([check["status"].upper(), check["name"], check["detail"]])
    summary = "%d failure(s), %d warning(s)" % (value["failures"], value["warnings"])
    return "%s\n\n%s" % (table(["STATUS", "CHECK", "DETAIL"], rows), summary)


def _firewall_text(value):
    status = value.get("status")
    ports = ", ".join(str(port) for port in value.get("ports") or [])
    backend = value.get("backend") or "firewall"
    if status == "opened":
        return "opened %s via %s" % (ports, backend)
    if status == "inactive":
        return "no active UFW/firewalld"
    if status == "skipped":
        return "skipped%s" % (" (%s)" % value["hint"] if value.get("hint") else "")
    if status == "failed":
        return "failed via %s for %s" % (backend, ports)
    return status or "unknown"


def _fmt_setup(value):
    if value.get("dry_run"):
        lines = [
            "Setup dry run (no changes made)",
            "Domains:     %s" % ", ".join(value.get("domains") or []),
            "Admin:       %s" % value.get("admin"),
            "Web server:  %s" % (value.get("webserver") or "none"),
            "Storage:     %s" % (value.get("storage") or "internal_hashed"),
            "Proxy HTTP:  %s" % ("yes" if value.get("proxy") else "no"),
            "certbot:     %s" % ("yes" if value.get("certbot") else "no"),
            "Install:     %s" % ("Prosody is missing" if value.get("install_prosody")
                                  else "Prosody is already installed"),
            "Packages:    %s" % value.get("packages"),
            "TURN:        %s" % (
                (value.get("turn") or {}).get("realm") or "no"),
            "",
            value.get("prosody_config_preview", ""),
        ]
        if value.get("web_config_preview"):
            lines.extend(["", "# Web server config preview", value["web_config_preview"]])
        return "\n".join(lines)
    prosody = value.get("prosody") or {}
    lines = [
        "Prosody:     %s" % prosody.get("config_file"),
        "Data:        %s" % prosody.get("data_dir"),
        "Domains:     %s" % ", ".join(value.get("domains") or []),
        "Admin:       %s%s" % (
            value.get("admin"),
            " (created)" if value.get("admin_created") else " (create it later)"),
        "Web server:  %s" % (value.get("webserver", {}).get("kind")
                              if value.get("webserver") else "none"),
        "Storage:     %s" % (value.get("storage") or "internal_hashed"),
        "Web config:  %s" % (value.get("web_config") or "-"),
        "TLS:         %s" % ("yes (%s)" % value.get("cert_name")
                              if value.get("tls") else "no"),
        "certbot:     %s" % ("yes" if value.get("certbot") else "no"),
        "Proxy HTTP:  %s" % ("yes" if value.get("proxy") else "no"),
    ]
    if value.get("firewall"):
        lines.append("Firewall:    %s" % _firewall_text(value["firewall"]))
    turn = value.get("turn") or {}
    if turn:
        lines.append("TURN:        %s (realm %s, config %s)" % (
            "enabled" if turn.get("installed", {}).get("installed") else "skipped",
            turn.get("realm") or "-", turn.get("config_path") or "-"))
        if turn.get("validated") is False:
            lines.append("TURN warning: coturn rejected the generated configuration")
    if value.get("certbot_failed"):
        lines.append("WARNING: certbot did not produce a certificate; check DNS/port 80")
    return "\n".join(lines)


def _fmt_discover(value):
    host = value["host"]
    lines = [
        "Host:       %s (%s, %s)" % (host["pretty_name"] or host["system"],
                                      host["machine"], host["hostname"]),
        "Family:     %s" % host["family"],
        "Init:       %s" % host["init"],
        "Python:     %s" % host["python"],
        "Repository: %s" % value["repository"],
    ]
    prosody = value.get("prosody")
    if not prosody:
        lines.append("Prosody:    not installed/configured")
        lines.append("Installed:  %s" % ("yes (but no config found)"
                                         if value.get("prosody_installed") else "no"))
        lines.append("Web server: %s" % _web_description(value.get("webserver")))
        return "\n".join(lines)
    lines.extend([
        "Prosody:    %s" % (prosody.get("version") or "version unknown"),
        "Installed:  yes",
        "Web server: %s" % _web_description(value.get("webserver")),
        "Config:     %s" % prosody.get("config_file"),
        "Data:       %s" % prosody.get("data_dir"),
        "Logs:       %s" % prosody.get("log_dir"),
        "Certs:      %s" % ", ".join(prosody.get("cert_dirs") or []),
        "Plugins:    %s" % (", ".join(prosody.get("plugin_dirs") or []) or "-"),
        "Storage:    %s" % prosody.get("storage"),
        "Database:   %s" % _db_description(prosody.get("sql") or {}),
        "Service:    %s" % prosody.get("service"),
        "User:       %s:%s" % (prosody.get("user"), prosody.get("group")),
        "Hosts:      %s" % (", ".join(prosody.get("virtualhosts") or []) or "-"),
        "Components: %s" % (", ".join(prosody.get("components") or []) or "-"),
        "Modules:    %s" % (", ".join(prosody.get("modules_enabled") or []) or "-"),
    ])
    if prosody.get("warnings"):
        lines.append("Warnings:")
        lines.extend("  - %s" % item for item in prosody["warnings"])
    return "\n".join(lines)


def _web_description(web):
    if not web:
        return "none"
    return "%s (%s, proxied: %s)" % (
        web.get("kind"), "installed" if web.get("installed") else "not installed",
        "yes" if web.get("proxied") else "no")


def _db_description(sql):
    if not sql:
        return "none"
    engine = sql.get("engine") or sql.get("driver")
    if engine == "sqlite":
        return "SQLite3 (%s)" % sql.get("path")
    return "%s (%s@%s/%s)" % (engine, sql.get("username") or "default",
                              sql.get("host") or "localhost",
                              sql.get("database") or "prosody")


def _fmt_import(value):
    entry = value["entry"]
    lines = [
        "%s %s" % ("Imported" if value["imported"] else "Already present:", entry["id"]),
        "Archive: %s" % entry.get("archive"),
        "SHA-256: %s" % value["sha256"],
    ]
    if value.get("verify"):
        lines.append("Verification: %s" % (
            "OK" if not value["verify"]["errors"] else "; ".join(value["verify"]["errors"])))
    return "\n".join(lines)


def _fmt_prune(value):
    if not value["removed"]:
        return "Nothing to prune (dry run: %s)" % ("yes" if value["dry_run"] else "no")
    rows = []
    for item in value["removed"]:
        rows.append([item["id"], item["archive"], human_bytes(item["freed"])])
    header = "Would remove" if value["dry_run"] else "Removed"
    return "%s %d backup(s), freeing %s\n\n%s" % (
        header, len(value["removed"]), human_bytes(value["freed"]),
        table(["ID", "ARCHIVE", "SIZE"], rows),
    )


def _fmt_diff(value):
    if not value["differences"]:
        return "Backup %s matches the current installation." % value["id"]
    rows = [[item["field"], item["backup"], item["current"]]
            for item in value["differences"]]
    return "Backup %s differs from this installation:\n\n%s" % (
        value["id"], table(["FIELD", "BACKUP", "CURRENT"], rows))


def _fmt_history(value):
    if not value["history"]:
        return "No history in %s" % value["repository"]
    rows = []
    for item in value["history"]:
        details = item.get("details") or {}
        detail = details.get("id") or details.get("removed") or ""
        rows.append([item.get("time", ""), item.get("action", ""),
                     item.get("user") or "", shorten(str(detail), 40)])
    return table(["TIME", "ACTION", "USER", "DETAIL"], rows)


def _fmt_schedule_show(value):
    if not value["systemd"] and not value["cron"]:
        return "No Proctor schedule installed"
    lines = []
    for label in ("timer", "service"):
        item = value["systemd"].get(label)
        if item:
            lines.append("== %s ==" % item["path"])
            lines.append(item["content"].rstrip())
    if value["cron"]:
        lines.append("== %s ==" % value["cron"]["path"])
        lines.append(value["cron"]["content"].rstrip())
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
