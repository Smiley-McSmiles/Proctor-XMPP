#compdef proctor

_proctor() {
  local -a commands
  commands=(
    'init:create config and repository'
    'setup:install and configure Prosody and a web server'
    'discover:show detected OS and Prosody layout'
    'doctor:run preflight checks'
    'backup:create a backup'
    'list:list backups'
    'info:show a manifest'
    'verify:verify an archive'
    'users:list accounts'
    'restore:restore onto this host'
    'migrate:migrate to a new host'
    'export:export a portable bundle'
    'import:import a bundle'
    'diff:compare with this host'
    'prune:apply retention'
    'delete:delete a backup'
    'schedule:install/remove a schedule'
    'history:show audit history'
    'config:show configuration'
    'version:show version'
  )
  _arguments -C \
    '--config[config file]:file:_files' \
    '--repo[repository directory]:dir:_directories' \
    '--json[machine-readable output]' \
    '--quiet[quiet output]' \
    '--no-color[disable color]' \
    '--yes[assume yes]' \
    '1:command:->command' \
    '*::arg:->args'

  case $state in
    command)
      _describe 'command' commands
      ;;
    args)
      case $words[1] in
        backup)
          _arguments '--live[online backup]' '--verify[verify after writing]' '--encrypt[encrypt]' '--include-logs[include logs]' '--only[components]:components' '--exclude[components]:components' '--exclude-path[skip path]:path:_files' '--include-path[extra path]:path:_files'
          ;;
        restore|migrate)
          _arguments '--dry-run[show changes]' '--yes[assume yes]' '--keep-config[keep target config]' '--map[path map]:map' '--start[start service]' '--no-start[leave stopped]'
          ;;
      esac
      ;;
  esac
}

_proctor "$@"
