"""Consistent database dumps and restores for Prosody's supported backends."""

from __future__ import print_function

import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse

from .errors import ExternalCommandError, ProctorError
from .util import copy_file_atomic, ensure_dir, isoformat, run, sha256_file, which

DUMP_NAMES = {
    "sqlite": "database.sqlite",
    "postgres": "database.sql",
    "mysql": "database.sql",
}


def db_is_configured(sql, storage=None):
    if not sql:
        return False
    if storage == "sql":
        return True
    return bool(sql.get("configured"))


def dump_database(install, workdir, reporter=None):
    """Dump the configured database into workdir. Returns manifest metadata."""
    sql = install.sql
    if not db_is_configured(sql, install.storage):
        return None
    engine = sql.get("engine")
    ensure_dir(workdir, mode=0o700)
    if engine == "sqlite":
        return _dump_sqlite(sql, workdir, reporter)
    if engine == "postgres":
        return _dump_postgres(sql, workdir, reporter)
    if engine == "mysql":
        return _dump_mysql(sql, workdir, reporter)
    raise ProctorError(
        "unsupported SQL driver: %s" % sql.get("driver"),
        hint="Proctor supports SQLite3, PostgreSQL and MySQL/MariaDB",
    )


def _dump_sqlite(sql, workdir, reporter):
    source_path = sql.get("path") or sql.get("database")
    if not source_path or not os.path.isfile(source_path):
        raise ProctorError(
            "SQLite database not found: %s" % source_path,
            hint="check sql.database in the Prosody config, or use --skip-database",
        )
    destination = os.path.join(workdir, DUMP_NAMES["sqlite"])
    if reporter:
        reporter.info("Dumping SQLite database: %s" % source_path)
    uri = "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(source_path))
    source = None
    target = None
    try:
        source = sqlite3.connect(uri, uri=True, timeout=30.0)
        source.execute("PRAGMA busy_timeout = 30000")
        target = sqlite3.connect(destination)
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ProctorError("SQLite quick_check failed on the backup copy: %s" % (result,))
        users = _sqlite_users(target)
    except sqlite3.Error as exc:
        raise ProctorError("could not back up SQLite database %s: %s" % (source_path, exc))
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
    os.chmod(destination, 0o600)
    digest, size = sha256_file(destination)
    return {
        "driver": sql.get("driver") or "SQLite3",
        "engine": "sqlite",
        "configured": True,
        "path": os.path.abspath(source_path),
        "database": os.path.abspath(source_path),
        "host": None,
        "port": None,
        "username": None,
        "dump": "db/" + DUMP_NAMES["sqlite"],
        "format": "sqlite",
        "sha256": digest,
        "size": size,
        "user_count": len(users),
        "users": users[:200],
        "users_truncated": len(users) > 200,
    }


def _dump_postgres(sql, workdir, reporter):
    if not which("pg_dump"):
        raise ProctorError(
            "PostgreSQL storage is configured but pg_dump is not installed",
            hint="install postgresql-client, or use --skip-database",
        )
    destination = os.path.join(workdir, DUMP_NAMES["postgres"])
    command = ["pg_dump", "--clean", "--if-exists", "--no-owner", "--no-privileges",
               "--encoding=UTF8"]
    if sql.get("host"):
        command.extend(["--host", str(sql["host"])])
    if sql.get("port"):
        command.extend(["--port", str(sql["port"])])
    if sql.get("username"):
        command.extend(["--username", str(sql["username"])])
    command.extend(["--dbname", str(sql.get("database") or "prosody"), "--file", destination])
    env = _pg_env(sql)
    if reporter:
        reporter.info("Dumping PostgreSQL database: %s" % (sql.get("database") or "prosody"))
    run(command, env=env, timeout=None)
    os.chmod(destination, 0o600)
    digest, size = sha256_file(destination)
    users = _postgres_users(sql, env)
    return {
        "driver": sql.get("driver") or "PostgreSQL",
        "engine": "postgres",
        "configured": True,
        "path": None,
        "database": sql.get("database") or "prosody",
        "host": sql.get("host"),
        "port": sql.get("port"),
        "username": sql.get("username"),
        "dump": "db/" + DUMP_NAMES["postgres"],
        "format": "sql",
        "sha256": digest,
        "size": size,
        "user_count": len(users) if users is not None else None,
        "users": users[:200] if users else [],
        "users_truncated": bool(users and len(users) > 200),
    }


def _dump_mysql(sql, workdir, reporter):
    if not which("mysqldump"):
        raise ProctorError(
            "MySQL storage is configured but mysqldump is not installed",
            hint="install mysql-client/mariadb-client, or use --skip-database",
        )
    destination = os.path.join(workdir, DUMP_NAMES["mysql"])
    command = ["mysqldump", "--single-transaction", "--quick", "--routines", "--triggers",
               "--skip-comments", "--no-tablespaces"]
    if sql.get("host"):
        command.extend(["--host", str(sql["host"])])
    if sql.get("port"):
        command.extend(["--port", str(sql["port"])])
    if sql.get("username"):
        command.extend(["--user", str(sql["username"])])
    command.extend(["--result-file", destination, str(sql.get("database") or "prosody")])
    env = dict(os.environ)
    defaults_file = None
    if sql.get("password"):
        defaults_file = _mysql_defaults_file(sql)
        command.insert(1, "--defaults-extra-file=%s" % defaults_file)
    if reporter:
        reporter.info("Dumping MySQL database: %s" % (sql.get("database") or "prosody"))
    try:
        run(command, env=env, timeout=None)
    finally:
        if defaults_file:
            try:
                os.unlink(defaults_file)
            except OSError:
                pass
    os.chmod(destination, 0o600)
    digest, size = sha256_file(destination)
    users = _mysql_users(sql)
    return {
        "driver": sql.get("driver") or "MySQL",
        "engine": "mysql",
        "configured": True,
        "path": None,
        "database": sql.get("database") or "prosody",
        "host": sql.get("host"),
        "port": sql.get("port"),
        "username": sql.get("username"),
        "dump": "db/" + DUMP_NAMES["mysql"],
        "format": "sql",
        "sha256": digest,
        "size": size,
        "user_count": len(users) if users is not None else None,
        "users": users[:200] if users else [],
        "users_truncated": bool(users and len(users) > 200),
    }


def _pg_env(sql):
    env = dict(os.environ)
    if sql.get("password"):
        env["PGPASSWORD"] = str(sql["password"])
    env.setdefault("PGCONNECT_TIMEOUT", "15")
    return env


def _sqlite_users(connection):
    try:
        rows = connection.execute(
            "SELECT DISTINCT \"host\", \"user\" FROM \"prosody\" WHERE \"store\" = 'accounts' "
            "ORDER BY \"host\", \"user\""
        ).fetchall()
    except sqlite3.Error:
        return []
    result = []
    for host, user in rows:
        if host and user:
            result.append("%s@%s" % (user, host))
    return result


def sqlite_users_from_file(path):
    connection = sqlite3.connect(path)
    try:
        return _sqlite_users(connection)
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _psql_command(sql, extra=None):
    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
    if sql.get("host"):
        command.extend(["--host", str(sql["host"])])
    if sql.get("port"):
        command.extend(["--port", str(sql["port"])])
    if sql.get("username"):
        command.extend(["--username", str(sql["username"])])
    command.extend(["--dbname", str(sql.get("database") or "prosody")])
    if extra:
        command.extend(extra)
    return command


def _postgres_users(sql, env):
    if not which("psql"):
        return None
    query = ("SELECT DISTINCT \"user\", \"host\" FROM \"prosody\" "
             "WHERE \"store\" = 'accounts' ORDER BY \"host\", \"user\";")
    try:
        proc = run(_psql_command(sql, ["--tuples-only", "--no-align", "--command", query]),
                   env=env, check=False, timeout=25)
    except ExternalCommandError:
        return None
    if proc.returncode != 0:
        return None
    users = []
    for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
        if "|" in line:
            user, host = line.split("|", 1)
            if user and host:
                users.append("%s@%s" % (user, host))
    return users


def _mysql_defaults_file(sql):
    fd, path = tempfile.mkstemp(prefix="proctor-mysql-", suffix=".cnf")
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("[client]\n")
        if sql.get("username"):
            handle.write("user=%s\n" % sql["username"])
        if sql.get("password"):
            escaped = str(sql["password"]).replace("\\", "\\\\").replace('"', '\\"')
            handle.write('password="%s"\n' % escaped)
    return path


def _mysql_command(sql, extra=None):
    command = ["mysql"]
    if sql.get("host"):
        command.extend(["--host", str(sql["host"])])
    if sql.get("port"):
        command.extend(["--port", str(sql["port"])])
    if sql.get("username"):
        command.extend(["--user", str(sql["username"])])
    command.extend(["--database", str(sql.get("database") or "prosody")])
    if extra:
        command.extend(extra)
    return command


def _mysql_users(sql):
    if not which("mysql"):
        return None
    defaults_file = _mysql_defaults_file(sql) if sql.get("password") else None
    try:
        command = _mysql_command(sql, ["--batch", "--skip-column-names", "--execute",
                                       "SELECT DISTINCT `user`, `host` FROM `prosody` "
                                       "WHERE `store` = 'accounts' ORDER BY `host`, `user`;"])
        if defaults_file:
            command.insert(1, "--defaults-extra-file=%s" % defaults_file)
        proc = run(command, check=False, timeout=25)
    except ExternalCommandError:
        return None
    finally:
        if defaults_file:
            try:
                os.unlink(defaults_file)
            except OSError:
                pass
    if proc.returncode != 0:
        return None
    users = []
    for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
        if "\t" in line:
            user, host = line.split("\t", 1)
            if user and host:
                users.append("%s@%s" % (user, host))
    return users


def restore_database(metadata, install, dump_path, workdir, reporter=None, dry_run=False,
                     chown_callback=None, target_data_dir=None):
    """Restore a DB dump. install may be None for a fresh target."""
    if not metadata:
        return None
    engine = metadata.get("engine")
    target_sql = (install.sql if install is not None else {}) or {}
    target_engine = target_sql.get("engine") if target_sql else None
    if target_engine is None:
        if engine == "sqlite":
            target_engine = "sqlite"
            base_dir = (install.data_dir if install is not None
                        else target_data_dir) or "."
            database = os.path.join(base_dir, os.path.basename(
                metadata.get("path") or DUMP_NAMES["sqlite"]
            ))
            target_sql = {
                "engine": "sqlite",
                "path": database,
                "database": database,
            }
        else:
            raise ProctorError(
                "this backup contains a %s database, but no Prosody SQL configuration was "
                "found on the target" % engine,
                hint="install/configure Prosody on the target first, or restore only files",
            )
    if target_engine != engine:
        raise ProctorError(
            "database backend mismatch: backup is %s, target is %s" % (engine, target_engine),
            hint="restore the database with the same backend, or use Prosody's prosody-migrator "
                 "to convert between storage backends before/after restoring files",
        )

    if engine == "sqlite":
        return _restore_sqlite(metadata, target_sql, dump_path, dry_run, chown_callback)
    if engine == "postgres":
        return _restore_postgres(metadata, target_sql, dump_path, reporter, dry_run)
    if engine == "mysql":
        return _restore_mysql(metadata, target_sql, dump_path, reporter, dry_run)
    raise ProctorError("unsupported database engine in backup: %s" % engine)


def _restore_sqlite(metadata, target_sql, dump_path, dry_run, chown_callback):
    destination = target_sql.get("path") or target_sql.get("database")
    if not destination:
        raise ProctorError("no SQLite database path is configured on the target")
    destination = os.path.abspath(destination)
    actions = []
    if dry_run:
        return {"engine": "sqlite", "path": destination, "dry_run": True}
    parent = os.path.dirname(destination)
    ensure_dir(parent, mode=0o750)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for suffix in ("", "-wal", "-shm"):
        existing = destination + suffix
        if os.path.exists(existing):
            backup = "%s.pre-proctor-%s" % (existing, stamp)
            os.replace(existing, backup)
            actions.append(backup)
    copy_file_atomic(dump_path, destination, mode=0o640)
    connection = sqlite3.connect(destination)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ProctorError("restored SQLite database failed quick_check: %s" % (result,))
    finally:
        connection.close()
    if chown_callback:
        chown_callback(destination)
    return {"engine": "sqlite", "path": destination, "previous": actions}


def _restore_postgres(metadata, target_sql, dump_path, reporter, dry_run):
    if not which("psql"):
        raise ProctorError("restoring a PostgreSQL backup requires the psql client")
    command = _psql_command(target_sql, ["--file", os.path.abspath(dump_path)])
    if reporter:
        reporter.info("Restoring PostgreSQL database: %s" % (target_sql.get("database") or "prosody"))
    if dry_run:
        return {"engine": "postgres", "database": target_sql.get("database"), "dry_run": True}
    run(command, env=_pg_env(target_sql), timeout=None)
    return {"engine": "postgres", "database": target_sql.get("database")}


def _restore_mysql(metadata, target_sql, dump_path, reporter, dry_run):
    if not which("mysql"):
        raise ProctorError("restoring a MySQL backup requires the mysql client")
    command = _mysql_command(target_sql)
    defaults_file = _mysql_defaults_file(target_sql) if target_sql.get("password") else None
    if defaults_file:
        command.insert(1, "--defaults-extra-file=%s" % defaults_file)
    if reporter:
        reporter.info("Restoring MySQL database: %s" % (target_sql.get("database") or "prosody"))
    if dry_run:
        result = {"engine": "mysql", "database": target_sql.get("database"), "dry_run": True}
        if defaults_file:
            os.unlink(defaults_file)
        return result
    try:
        with open(dump_path, "rb") as handle:
            proc = subprocess.run(command, stdin=handle)
        if proc.returncode != 0:
            raise ExternalCommandError(command, proc.returncode, "mysql import failed")
    finally:
        if defaults_file:
            try:
                os.unlink(defaults_file)
            except OSError:
                pass
    return {"engine": "mysql", "database": target_sql.get("database")}


def describe_database(metadata):
    if not metadata:
        return "none"
    engine = metadata.get("engine")
    if engine == "sqlite":
        return "SQLite3 (%s)" % metadata.get("path")
    return "%s (%s@%s/%s)" % (
        metadata.get("driver") or engine,
        metadata.get("username") or "default",
        metadata.get("host") or "localhost",
        metadata.get("database") or "prosody",
    )
