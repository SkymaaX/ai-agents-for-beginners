#!/bin/sh
# Installe les skills du « Top 10 Claude Code », un par un, avec ta confirmation.
#
#   ./installer-skills.sh                     propose chaque skill (réponds o ou n)
#   ./installer-skills.sh caveman ponytail    seulement ceux-là
#   ./installer-skills.sh --simulation        affiche les commandes sans rien lancer
#   ./installer-skills.sh --oui               ne pose aucune question (après avoir lu --liste)
#   ./installer-skills.sh --liste             sources et effets de chaque skill
#
# Règles : aucune commande sans ton accord ; pas de sudo ; pas de « curl | sh » ; la config du
# routeur n'est pas touchée. Chaque skill est du code tiers : plusieurs ajoutent des hooks qui
# s'exécutent au démarrage de chaque session Claude Code.
#
# Les noms de plugins et de marketplaces viennent des fichiers officiels
# (.claude-plugin/marketplace.json de chaque dépôt) ; voir README.md, section « Skills Claude Code ».

set -u

DRY_RUN=0
ASSUME_YES=0
SELECTED=""
RESULT_INSTALLED=""
RESULT_PRESENT=""
RESULT_SKIPPED=""
RESULT_FAILED=""

ALL_IDS="superpowers ponytail ui-ux-pro-max graphify caveman agent-skills understand-anything awesome-claude-skills archify impeccable"

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
}

print_list() {
    cat <<'EOF'
superpowers            obra/superpowers                          plan, TDD, debug, revue. Hook SessionStart.
ponytail               DietrichGebert/ponytail                   code minimal. Hooks Node.js.
ui-ux-pro-max          nextlevelbuilder/ui-ux-pro-max-skill      design UI/UX. Lance un script Python local.
graphify               paquet PyPI « graphifyy » (deux y)        graphe de connaissance du code. Via uv ou pipx.
caveman                JuliusBrussee/caveman                     réponses très courtes dans toutes tes sessions.
agent-skills           addyosmani/agent-skills                   bonnes pratiques d'ingénierie (Addy Osmani).
understand-anything    Egonex-AI/Understand-Anything             graphe interactif d'un projet.
awesome-claude-skills  ComposioHQ/awesome-claude-skills          LISTE de skills, pas un skill : jamais installée en bloc.
archify                archify.si (via npx skills)               schémas d'architecture depuis le code.
impeccable             pbakaus/impeccable                        finition et revue de design frontend.
EOF
}

# --- Aides communes ----------------------------------------------------------------------------

# Affiche la commande puis la lance (sauf en simulation).
run() {
    printf '  $ %s\n' "$*"
    [ "$DRY_RUN" = 1 ] && return 0
    "$@"
}

# Pose la question ; toute réponse autre que o/oui (ou la fin de l'entrée) vaut non.
confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    printf '%s [o/N] ' "$1"
    answer=""
    if ! read -r answer; then
        printf '\n'
        return 1
    fi
    case "$answer" in
        o | O | oui | Oui | OUI | y | Y | yes | Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# need <commande> <aide> : vérifie un prérequis ; en simulation, signale seulement.
need() {
    have "$1" && return 0
    if [ "$DRY_RUN" = 1 ]; then
        say "  (manque : $1, ignoré en simulation)"
        return 0
    fi
    err "  Manque : $1. $2"
    return 1
}

plugin_present() {
    have claude || return 1
    claude plugin list 2>/dev/null | grep -Eq "(^|[^[:alnum:]_-])$1@"
}

marketplace_known() {
    have claude || return 1
    claude plugin marketplace list 2>/dev/null | grep -Fq "$1"
}

header() {
    say ""
    say "── $1"
}

note_installed() { RESULT_INSTALLED="$RESULT_INSTALLED $1"; }
note_present() { RESULT_PRESENT="$RESULT_PRESENT $1"; }
note_skipped() { RESULT_SKIPPED="$RESULT_SKIPPED $1"; }
note_failed() { RESULT_FAILED="$RESULT_FAILED $1"; }

# --- Plugins Claude Code (marketplace GitHub) -----------------------------------------------------

# install_plugin <id> <titre> <dépôt> <marketplace> <plugin> <commande requise ou ""> <à savoir>
install_plugin() {
    ip_id=$1 ip_title=$2 ip_repo=$3 ip_market=$4 ip_plugin=$5 ip_need=$6 ip_warning=$7
    header "$ip_title"
    say "  Source : https://github.com/$ip_repo"
    say "  À savoir : $ip_warning"
    if ! need claude "Installe Claude Code, lance-le une fois, puis relance ce script."; then
        note_failed "$ip_id"
        return
    fi
    if [ -n "$ip_need" ] && ! need "$ip_need" "Ce skill en dépend."; then
        note_failed "$ip_id"
        return
    fi
    if plugin_present "$ip_plugin"; then
        say "  Déjà installé."
        note_present "$ip_id"
        return
    fi
    if ! confirm "  Installer $ip_title ?"; then
        say "  Ignoré."
        note_skipped "$ip_id"
        return
    fi
    # `marketplace add` échoue si la source est déjà connue : on continue alors si elle est listée.
    if run claude plugin marketplace add "$ip_repo" || marketplace_known "$ip_market"; then
        if run claude plugin install "$ip_plugin@$ip_market"; then
            note_installed "$ip_id"
            return
        fi
    fi
    err "  Échec de l'installation de $ip_title."
    note_failed "$ip_id"
}

# --- Graphify (paquet Python, hors marketplace) -----------------------------------------------------

install_graphify() {
    header "Graphify"
    say "  Source : https://github.com/safishamsi/graphify, paquet PyPI « graphifyy » (deux y ; les autres noms ne sont pas officiels)."
    say "  À savoir : installe un programme Python (isolé par uv ou pipx) puis enregistre le skill chez toi."
    say "  Selon sa documentation, il peut aussi ajouter des règles et un hook avant les recherches de fichiers :"
    say "  lis https://docs.graphify.com/guides/always-on avant de dire oui."
    if have graphify; then
        say "  Programme déjà présent : seule l'étape « graphify install » sera lancée."
        gf_tool=""
    elif have uv; then
        gf_tool="uv tool install graphifyy"
    elif have pipx; then
        gf_tool="pipx install graphifyy"
    elif [ "$DRY_RUN" = 1 ]; then
        say "  (manque : uv ou pipx, ignoré en simulation)"
        gf_tool="uv tool install graphifyy"
    else
        err "  Manque : uv ou pipx. Debian / Ubuntu : sudo apt install pipx   (ce script n'installe pas uv à ta place)."
        note_failed graphify
        return
    fi
    if [ -n "$gf_tool" ] && [ "${gf_tool%% *}" = "pipx" ] && have python3 \
        && ! python3 -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)'; then
        err "  Graphify demande Python 3.10 à 3.14 : prends plutôt uv, qui sait récupérer le bon Python."
        note_failed graphify
        return
    fi
    if ! confirm "  Installer Graphify ?"; then
        say "  Ignoré."
        note_skipped graphify
        return
    fi
    if [ -n "$gf_tool" ]; then
        # shellcheck disable=SC2086  # la commande est un mot par argument, sans espace dans les valeurs
        if ! run $gf_tool; then
            err "  Échec de l'installation de Graphify."
            note_failed graphify
            return
        fi
    fi
    if ! have graphify && [ "$DRY_RUN" != 1 ]; then
        err "  Programme installé mais « graphify » reste introuvable : lance « uv tool update-shell » (ou « pipx ensurepath »),"
        err "  rouvre le terminal, puis lance « graphify install »."
        note_failed graphify
        return
    fi
    if run graphify install; then
        note_installed graphify
    else
        err "  Échec de « graphify install »."
        note_failed graphify
    fi
}

# --- Archify (paquet npm « skills ») ------------------------------------------------------------------

install_archify() {
    header "Archify"
    say "  Source : https://github.com/tt-a1i/archify (installation officielle du README : https://archify.si)."
    say "  À savoir : npx télécharge le paquet npm « skills » puis le skill. Archify peut interroger son site pour afficher"
    say "  un rappel de mise à jour (désactivable : ARCHIFY_UPDATE_CHECK_DISABLED=1)."
    if ! need npx "Installe Node.js (npm)."; then
        note_failed archify
        return
    fi
    if ! confirm "  Installer Archify ?"; then
        say "  Ignoré."
        note_skipped archify
        return
    fi
    if run npx -y skills@latest add https://archify.si --skill archify --agent claude-code --global --copy --yes; then
        note_installed archify
    else
        err "  Échec de l'installation d'Archify."
        note_failed archify
    fi
}

# --- Awesome Claude Skills : une liste, rien à installer ------------------------------------------------

info_awesome() {
    header "Awesome Claude Skills"
    say "  Ce n'est pas un skill : c'est une liste de plus de mille entrées (https://github.com/ComposioHQ/awesome-claude-skills)."
    say "  La cloner dans ~/.claude/skills chargerait des centaines de skills dans chaque session."
    say "  Parcours la liste, choisis un skill précis, puis installe-le seul. Rien n'a été fait."
    note_skipped awesome-claude-skills
}

# --- Orchestration -----------------------------------------------------------------------------------

install_one() {
    case "$1" in
        superpowers)
            install_plugin superpowers "Superpowers" obra/superpowers superpowers-dev superpowers "" \
                "ajoute un hook SessionStart : des consignes sont injectées au début de chaque session." ;;
        ponytail)
            install_plugin ponytail "Ponytail" DietrichGebert/ponytail ponytail ponytail node \
                "ses hooks tournent avec Node.js (sans Node dans le PATH ils ne font rien)." ;;
        ui-ux-pro-max)
            install_plugin ui-ux-pro-max "UI UX Pro Max" nextlevelbuilder/ui-ux-pro-max-skill ui-ux-pro-max-skill ui-ux-pro-max python3 \
                "le skill lance un script Python local (search.py) pour chercher styles et palettes." ;;
        graphify) install_graphify ;;
        caveman)
            install_plugin caveman "Caveman" JuliusBrussee/caveman caveman caveman "" \
                "toutes tes réponses Claude Code deviennent très courtes tant qu'il est actif (« stop caveman » pour revenir)." ;;
        agent-skills)
            install_plugin agent-skills "Addy Osmani Skills" addyosmani/agent-skills addy-agent-skills agent-skills "" \
                "collection de skills d'ingénierie (spec, plan, build, test, revue, livraison) qui s'activent selon la tâche." ;;
        understand-anything)
            install_plugin understand-anything "Understand Anything" Egonex-AI/Understand-Anything understand-anything understand-anything "" \
                "plusieurs dépôts portent ce nom (copies, forks) : celui-ci est celui dont le manifeste est signé « Egonex »." ;;
        awesome-claude-skills) info_awesome ;;
        archify) install_archify ;;
        impeccable)
            install_plugin impeccable "Impeccable" pbakaus/impeccable impeccable impeccable "" \
                "design frontend : commandes de polish, audit, critique ; dépôt officiel de Paul Bakaus (les forks existent)." ;;
    esac
}

is_known_id() {
    for known in $ALL_IDS; do
        [ "$known" = "$1" ] && return 0
    done
    return 1
}

summary() {
    say ""
    say "── Bilan"
    if [ "$DRY_RUN" = 1 ]; then
        say "  Simulation : aucune commande n'a été exécutée."
        [ -n "$RESULT_INSTALLED" ] && say "  Seraient installés :$RESULT_INSTALLED"
    else
        [ -n "$RESULT_INSTALLED" ] && say "  Installés :$RESULT_INSTALLED"
    fi
    [ -n "$RESULT_PRESENT" ] && say "  Déjà là :$RESULT_PRESENT"
    [ -n "$RESULT_SKIPPED" ] && say "  Ignorés :$RESULT_SKIPPED"
    [ -n "$RESULT_FAILED" ] && say "  Échecs :$RESULT_FAILED"
    if [ -n "$RESULT_INSTALLED" ] && [ "$DRY_RUN" != 1 ]; then
        say ""
        say "  Relance Claude Code pour activer les plugins. Aucun n'est branché dans le routeur :"
        say "  ses agents claude_cli ignorent tes plugins personnels exprès (voir README, « Skills Claude Code »)."
    fi
}

for arg in "$@"; do
    case "$arg" in
        --simulation) DRY_RUN=1 ;;
        --oui) ASSUME_YES=1 ;;
        --liste) print_list; exit 0 ;;
        -h | --aide | --help) usage; exit 0 ;;
        -*) err "Option inconnue : $arg"; usage >&2; exit 2 ;;
        *)
            if ! is_known_id "$arg"; then
                err "Skill inconnu : $arg"
                err "Choix possibles : $ALL_IDS"
                exit 2
            fi
            SELECTED="$SELECTED $arg" ;;
    esac
done
[ -z "$SELECTED" ] && SELECTED="$ALL_IDS"

say "Installation des skills Claude Code. Rien ne se lance sans ton « o »."
[ "$DRY_RUN" = 1 ] && say "(simulation : les commandes sont affichées, jamais exécutées)"

for skill in $SELECTED; do
    install_one "$skill"
done

summary
[ -z "$RESULT_FAILED" ]
