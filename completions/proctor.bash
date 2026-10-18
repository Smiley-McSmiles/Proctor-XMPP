# Bash completion for proctor
_proctor() {
    local cur prev words cword
    _init_completion 2>/dev/null || {
        COMPREPLY=()
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
    }

    local commands="init setup discover doctor backup create list ls info verify users restore migrate export import diff prune delete rm schedule history config version"
    local global_opts="--config --repo --json --quiet --verbose --no-color --yes --help --version"
    local components="config certs data plugins letsencrypt logs system database"

    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$commands $global_opts" -- "$cur") )
        return 0
    fi

    local command="${COMP_WORDS[1]}"
    case "$command" in
        backup|create)
            COMPREPLY=( $(compgen -W "--live --include-logs --no-logs --include-system --no-system --letsencrypt --compression --encrypt --no-encrypt --verify --skip-database --skip-unreadable --note --tag --only --exclude --exclude-path --include-path --prosody-config $global_opts" -- "$cur") )
            ;;
        restore|migrate)
            COMPREPLY=( $(compgen -W "--dry-run --only --exclude --keep-config --skip-database --no-verify --no-safety-backup --start --no-start --map --no-rewrite-config --no-overwrite --force --install-missing --no-install-missing --webserver --certbot --email --admin --proxy-http --no-proxy-http --prosody-config $global_opts" -- "$cur") )
            ;;
        list|ls)
            COMPREPLY=( $(compgen -W "--limit $global_opts" -- "$cur") )
            ;;
        verify)
            COMPREPLY=( $(compgen -W "--quick $global_opts" -- "$cur") )
            ;;
        users)
            COMPREPLY=( $(compgen -W "--domain --limit --all $global_opts" -- "$cur") )
            ;;
        export)
            COMPREPLY=( $(compgen -W "--output --force $global_opts" -- "$cur") )
            ;;
        import)
            COMPREPLY=( $(compgen -W "--manifest --verify $global_opts" -- "$cur") )
            ;;
        prune)
            COMPREPLY=( $(compgen -W "--keep-last --keep-daily --keep-weekly --keep-monthly --apply $global_opts" -- "$cur") )
            ;;
        delete|rm)
            COMPREPLY=( $(compgen -W "--dry-run $global_opts" -- "$cur") )
            ;;
        schedule)
            COMPREPLY=( $(compgen -W "--install --remove --show --at --type --cron-file --live --command $global_opts" -- "$cur") )
            ;;
        history)
            COMPREPLY=( $(compgen -W "--limit $global_opts" -- "$cur") )
            ;;
        doctor)
            COMPREPLY=( $(compgen -W "--no-config-check $global_opts" -- "$cur") )
            ;;
        init)
            COMPREPLY=( $(compgen -W "--path --force --print --setup --domain --webserver --certbot --email --prosody-config $global_opts" -- "$cur") )
            ;;
        setup)
            COMPREPLY=( $(compgen -W "--domain --admin --webserver --certbot --email --storage --proxy-http --no-proxy-http --no-start --dry-run --force --prosody-config $global_opts" -- "$cur") )
            ;;
        *)
            COMPREPLY=( $(compgen -W "$global_opts" -- "$cur") )
            ;;
    esac
    if [ "$prev" = "--only" ] || [ "$prev" = "--exclude" ]; then
        COMPREPLY=( $(compgen -W "$components" -- "$cur") )
    fi
}
complete -F _proctor proctor
