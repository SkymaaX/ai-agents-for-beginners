#!/bin/sh
# Ajoute à Claude Code les 4 connecteurs de la vidéo « Arrête Claude sans ses 4 connecteurs ».
#
#   ./installer-connecteurs.sh                       propose chaque connecteur (réponds o ou n)
#   ./installer-connecteurs.sh playwright            seulement celui-là
#   ./installer-connecteurs.sh --simulation          affiche les commandes sans rien lancer
#   ./installer-connecteurs.sh --oui                 ne pose aucune question de confirmation
#   ./installer-connecteurs.sh --liste               sources et effets de chaque connecteur
#
# Règles : aucune commande sans ton accord ; pas de sudo ; pas de « curl | sh » ; les clés API ne sont
# jamais affichées, jamais écrites dans un fichier du projet. Une clé est lue dans la variable
# d'environnement ou saisie sans écho ; Claude Code la range ensuite dans sa propre configuration
# (~/.claude.json), en clair : c'est son fonctionnement, pas celui de ce script.
#
# Les commandes viennent des README officiels de chaque projet (voir README.md, section « Connecteurs Claude »).

set -u

DRY_RUN=0
ASSUME_YES=0
SELECTED=""
RESULT_INSTALLED=""
RESULT_PRESENT=""
RESULT_SKIPPED=""
RESULT_FAILED=""
SECRET_VALUE=""

# firecrawl-compte n'est jamais proposé par défaut : il ne s'installe que si tu le nommes.
DEFAULT_IDS="perplexity firecrawl playwright composio"
ALL_IDS="perplexity firecrawl firecrawl-compte playwright composio"

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
}

print_list() {
    cat <<'EOF'
perplexity        recherche web en temps réel          clé API Perplexity requise (npx @perplexity-ai/mcp-server)
firecrawl         lire et extraire n'importe quel site  gratuit sans clé, débit limité (3 outils)
firecrawl-compte  idem avec ton compte Firecrawl        connexion par navigateur (OAuth), tous les outils. Jamais par défaut.
playwright        piloter un navigateur (formulaires, tests)  aucune clé (npx @playwright/mcp)
composio          connecter des centaines d'applications  PAS installé par ce script : voir l'explication.
EOF
}

# --- Aides communes ----------------------------------------------------------------------------

run() {
    printf '  $ %s\n' "$*"
    [ "$DRY_RUN" = 1 ] && return 0
    "$@"
}

# run_masked <affichage> <commande...> : affiche le texte donné (sans secret), lance la commande.
run_masked() {
    printf '  $ %s\n' "$1"
    shift
    [ "$DRY_RUN" = 1 ] && return 0
    "$@"
}

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

need() {
    have "$1" && return 0
    if [ "$DRY_RUN" = 1 ]; then
        say "  (manque : $1, ignoré en simulation)"
        return 0
    fi
    err "  Manque : $1. $2"
    return 1
}

# Saisie sans écho dans SECRET_VALUE ; l'écho du terminal est toujours rétabli.
prompt_secret() {
    SECRET_VALUE=""
    if have stty; then
        stty -echo 2>/dev/null
        trap 'stty echo 2>/dev/null; exit 130' INT TERM
    fi
    printf '%s' "$1"
    read -r SECRET_VALUE || SECRET_VALUE=""
    if have stty; then
        stty echo 2>/dev/null
        trap - INT TERM
    fi
    printf '\n'
}

mcp_present() {
    have claude || return 1
    claude mcp get "$1" >/dev/null 2>&1
}

header() {
    say ""
    say "── $1"
}

note_installed() { RESULT_INSTALLED="$RESULT_INSTALLED $1"; }
note_present() { RESULT_PRESENT="$RESULT_PRESENT $1"; }
note_skipped() { RESULT_SKIPPED="$RESULT_SKIPPED $1"; }
note_failed() { RESULT_FAILED="$RESULT_FAILED $1"; }

# begin <id> <titre> <serveur> <commande requise ou ""> <source> <à savoir>
# Affiche la fiche et vérifie les prérequis. Retourne 1 si l'installation ne doit pas continuer.
begin() {
    header "$2"
    say "  Source : $5"
    say "  À savoir : $6"
    if ! need claude "Installe Claude Code, lance-le une fois, puis relance ce script."; then
        note_failed "$1"
        return 1
    fi
    if [ -n "$4" ] && ! need "$4" "Ce connecteur en dépend."; then
        note_failed "$1"
        return 1
    fi
    if mcp_present "$3"; then
        say "  Le serveur « $3 » est déjà configuré : rien à faire (« claude mcp remove $3 » pour le refaire)."
        note_present "$1"
        return 1
    fi
    return 0
}

# --- Connecteurs ---------------------------------------------------------------------------------

install_playwright() {
    begin playwright "Playwright" playwright npx "https://github.com/microsoft/playwright-mcp" \
        "pilote un vrai navigateur sur ta machine (pages, clics, formulaires, tests). Aucune clé." || return
    if ! confirm "  Ajouter Playwright à Claude Code ?"; then
        say "  Ignoré."
        note_skipped playwright
        return
    fi
    if run claude mcp add --scope user playwright -- npx @playwright/mcp@latest; then
        note_installed playwright
    else
        err "  Échec de l'ajout de Playwright."
        note_failed playwright
    fi
}

# install_firecrawl <id> <titre> <adresse> <à savoir> <suite>
install_firecrawl() {
    begin "$1" "$2" firecrawl "" "https://github.com/firecrawl/firecrawl-mcp-server" "$4" || return
    if ! confirm "  Ajouter $2 à Claude Code ?"; then
        say "  Ignoré."
        note_skipped "$1"
        return
    fi
    if run claude mcp add --scope user --transport http firecrawl "$3"; then
        note_installed "$1"
        [ -n "$5" ] && say "  $5"
    else
        err "  Échec de l'ajout de $2."
        note_failed "$1"
    fi
}

install_perplexity() {
    begin perplexity "Perplexity" perplexity npx "https://github.com/perplexityai/modelcontextprotocol" \
        "exige une clé API Perplexity de ton compte (l'API est un service à part, vérifie sa facturation). La clé reste dans l'environnement ou dans ~/.claude.json, jamais dans le projet." || return
    if ! confirm "  Ajouter Perplexity à Claude Code ?"; then
        say "  Ignoré."
        note_skipped perplexity
        return
    fi
    px_key="${PERPLEXITY_API_KEY:-}"
    if [ -z "$px_key" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            px_key="<ta clé>"
        elif [ -t 0 ]; then
            prompt_secret "  Clé API Perplexity (invisible) : "
            px_key=$SECRET_VALUE
        fi
    fi
    if [ -z "$px_key" ]; then
        err "  Pas de clé : exporte PERPLEXITY_API_KEY puis relance (ou lance ce script dans un terminal pour la saisir)."
        note_failed perplexity
        return
    fi
    if run_masked "claude mcp add --scope user perplexity -e PERPLEXITY_API_KEY=*** -- npx -y @perplexity-ai/mcp-server" \
        claude mcp add --scope user perplexity -e "PERPLEXITY_API_KEY=$px_key" -- npx -y @perplexity-ai/mcp-server; then
        note_installed perplexity
    else
        err "  Échec de l'ajout de Perplexity."
        note_failed perplexity
    fi
    px_key=""
}

info_composio() {
    header "Composio"
    say "  Source : https://github.com/ComposioHQ/composio"
    say "  Pas installé par ce script, faute de commande vérifiée pour Claude Code. Ce que dit le README officiel :"
    say "    1. un point d'accès MCP hébergé, créé par session avec du code et une clé COMPOSIO_API_KEY (tableau de bord Composio) ;"
    say "    2. la CLI « composio », installée par « curl -fsSL https://composio.dev/install | sh »."
    say "  Ce script n'exécute jamais un « curl | sh ». Celui-ci modifie en plus ~/.bashrc et installe des plugins Claude Code"
    say "  détectés (option --no-plugins pour les éviter) : lis INSTALL.md avant de le lancer toi-même :"
    say "  https://github.com/ComposioHQ/composio/blob/HEAD/INSTALL.md"
    say "  Sa documentation (docs.composio.dev) était illisible lors de la vérification : le « +200 applications » de la vidéo"
    say "  n'est pas confirmé (le README annonce plus de 1000 applications)."
    note_skipped composio
}

# --- Orchestration -----------------------------------------------------------------------------------

install_one() {
    case "$1" in
        perplexity) install_perplexity ;;
        firecrawl)
            install_firecrawl firecrawl "Firecrawl (gratuit, sans clé)" https://mcp.firecrawl.dev/v2/mcp \
                "offre gratuite sans clé : seulement scrape, search et parse, débit limité. Pour tous les outils : ./installer-connecteurs.sh firecrawl-compte." "" ;;
        firecrawl-compte)
            install_firecrawl firecrawl-compte "Firecrawl (ton compte)" https://mcp.firecrawl.dev/v2/mcp-oauth \
                "connexion à ton compte Firecrawl par le navigateur (OAuth), sans clé à coller. Remplace la version sans clé : « claude mcp remove firecrawl » d'abord." \
                "Termine la connexion dans Claude Code : tape /mcp, choisis firecrawl, puis connecte ton compte." ;;
        playwright) install_playwright ;;
        composio) info_composio ;;
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
        [ -n "$RESULT_INSTALLED" ] && say "  Seraient ajoutés :$RESULT_INSTALLED"
    else
        [ -n "$RESULT_INSTALLED" ] && say "  Ajoutés :$RESULT_INSTALLED"
    fi
    [ -n "$RESULT_PRESENT" ] && say "  Déjà là :$RESULT_PRESENT"
    [ -n "$RESULT_SKIPPED" ] && say "  Ignorés :$RESULT_SKIPPED"
    [ -n "$RESULT_FAILED" ] && say "  Échecs :$RESULT_FAILED"
    if [ -n "$RESULT_INSTALLED" ] && [ "$DRY_RUN" != 1 ]; then
        say ""
        say "  Relance Claude Code, puis tape /mcp pour vérifier que les serveurs sont connectés."
        say "  Rien n'est branché dans le routeur (ses agents claude_cli ne chargent que la config du projet)."
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
                err "Connecteur inconnu : $arg"
                err "Choix possibles : $ALL_IDS"
                exit 2
            fi
            SELECTED="$SELECTED $arg" ;;
    esac
done
[ -z "$SELECTED" ] && SELECTED="$DEFAULT_IDS"

say "Ajout de connecteurs à Claude Code. Rien ne se lance sans ton « o »."
[ "$DRY_RUN" = 1 ] && say "(simulation : les commandes sont affichées, jamais exécutées)"

for connector in $SELECTED; do
    install_one "$connector"
done

summary
[ -z "$RESULT_FAILED" ]
