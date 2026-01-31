#!/bin/bash
#===============================================================================
# Hytale Dashboard - Installation Script
# https://github.com/zonfacter/hytale-dashboard
#
# Dieses Script installiert:
# - Hytale Dedicated Server Umgebung
# - Hytale Dashboard (Web-Interface)
# - Alle benoetigten Abhaengigkeiten und Services
#===============================================================================

set -e

# Farben fuer Ausgabe
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Konfiguration
HYTALE_USER="hytale"
HYTALE_WEB_USER="hytale-web"
HYTALE_DIR="/opt/hytale-server"
DASHBOARD_DIR="/opt/hytale-dashboard"
DASHBOARD_PORT="8088"
HYTALE_PORT="5520"
GITHUB_REPO="https://github.com/zonfacter/hytale-dashboard.git"

# Variablen fuer Benutzereingaben
DASH_PASSWORD=""
CF_API_KEY=""

#===============================================================================
# Hilfsfunktionen
#===============================================================================

print_header() {
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}  $1"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

print_step() {
    echo -e "${BLUE}▶${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_info() {
    echo -e "${CYAN}ℹ${NC} $1"
}

ask_yes_no() {
    local prompt="$1"
    local default="${2:-y}"
    local answer

    if [ "$default" = "y" ]; then
        prompt="$prompt [J/n]: "
    else
        prompt="$prompt [j/N]: "
    fi

    read -p "$prompt" answer
    answer=${answer:-$default}

    case "$answer" in
        [jJyY]*) return 0 ;;
        *) return 1 ;;
    esac
}

ask_input() {
    local prompt="$1"
    local default="$2"
    local result

    if [ -n "$default" ]; then
        read -p "$prompt [$default]: " result
        echo "${result:-$default}"
    else
        read -p "$prompt: " result
        echo "$result"
    fi
}

ask_password() {
    local prompt="$1"
    local password
    local password2

    while true; do
        read -s -p "$prompt: " password
        echo ""
        read -s -p "Passwort wiederholen: " password2
        echo ""

        if [ "$password" = "$password2" ]; then
            if [ ${#password} -lt 8 ]; then
                print_warning "Passwort sollte mindestens 8 Zeichen haben!"
                if ask_yes_no "Trotzdem verwenden?"; then
                    break
                fi
            else
                break
            fi
        else
            print_error "Passwoerter stimmen nicht ueberein!"
        fi
    done

    echo "$password"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "Dieses Script muss als root ausgefuehrt werden!"
        echo "Bitte mit 'sudo $0' starten."
        exit 1
    fi
}

check_os() {
    if [ ! -f /etc/os-release ]; then
        print_error "Konnte Betriebssystem nicht erkennen!"
        exit 1
    fi

    source /etc/os-release

    case "$ID" in
        debian|ubuntu)
            print_success "Betriebssystem: $PRETTY_NAME"
            ;;
        *)
            print_warning "Nicht getestetes Betriebssystem: $PRETTY_NAME"
            if ! ask_yes_no "Trotzdem fortfahren?"; then
                exit 1
            fi
            ;;
    esac
}

#===============================================================================
# Installations-Schritte
#===============================================================================

install_dependencies() {
    print_header "Installiere Abhaengigkeiten"

    print_step "Aktualisiere Paketliste..."
    apt-get update -qq

    print_step "Installiere benoetigte Pakete..."
    apt-get install -y -qq \
        python3 \
        python3-venv \
        python3-pip \
        git \
        curl \
        unzip \
        openjdk-21-jre-headless \
        ufw \
        > /dev/null

    print_success "Alle Abhaengigkeiten installiert"
}

create_users() {
    print_header "Erstelle Benutzer"

    # Hytale Server User
    if id "$HYTALE_USER" &>/dev/null; then
        print_info "Benutzer '$HYTALE_USER' existiert bereits"
    else
        print_step "Erstelle Benutzer '$HYTALE_USER'..."
        useradd -r -m -d "$HYTALE_DIR" -s /bin/bash "$HYTALE_USER"
        print_success "Benutzer '$HYTALE_USER' erstellt"
    fi

    # Dashboard User
    if id "$HYTALE_WEB_USER" &>/dev/null; then
        print_info "Benutzer '$HYTALE_WEB_USER' existiert bereits"
    else
        print_step "Erstelle Benutzer '$HYTALE_WEB_USER'..."
        useradd -r -s /usr/sbin/nologin "$HYTALE_WEB_USER"
        print_success "Benutzer '$HYTALE_WEB_USER' erstellt"
    fi

    # Gruppen-Mitgliedschaften
    print_step "Konfiguriere Gruppen..."
    usermod -aG "$HYTALE_USER" "$HYTALE_WEB_USER"
    usermod -aG systemd-journal "$HYTALE_WEB_USER"
    print_success "Gruppen konfiguriert"
}

setup_server_directories() {
    print_header "Erstelle Server-Verzeichnisse"

    # Note: Universe path changed in Hytale Server 2026.01 to Server/universe/
    print_step "Erstelle Verzeichnisstruktur..."
    mkdir -p "$HYTALE_DIR"/{backups,mods,Server/universe/worlds/default,.downloader}

    print_step "Setze Berechtigungen..."
    chown -R "$HYTALE_USER:$HYTALE_USER" "$HYTALE_DIR"
    chmod 750 "$HYTALE_DIR"

    # Mods-Verzeichnis fuer Dashboard beschreibbar
    chmod g+w "$HYTALE_DIR/mods"

    print_success "Server-Verzeichnisse erstellt"
}

install_dashboard() {
    print_header "Installiere Dashboard"

    if [ -d "$DASHBOARD_DIR/.git" ]; then
        print_step "Dashboard existiert, aktualisiere..."
        cd "$DASHBOARD_DIR"
        git pull --quiet
    else
        print_step "Klone Dashboard von GitHub..."
        if [ -d "$DASHBOARD_DIR" ]; then
            rm -rf "$DASHBOARD_DIR"
        fi
        git clone --quiet "$GITHUB_REPO" "$DASHBOARD_DIR"
    fi

    print_step "Setze Berechtigungen..."
    chown -R "$HYTALE_WEB_USER:$HYTALE_WEB_USER" "$DASHBOARD_DIR"

    print_step "Erstelle Python Virtual Environment..."
    sudo -u "$HYTALE_WEB_USER" bash -c "
        cd '$DASHBOARD_DIR'
        python3 -m venv .venv
        .venv/bin/pip install --quiet --upgrade pip
        .venv/bin/pip install --quiet -r requirements.txt
    "

    print_success "Dashboard installiert"
}

install_wrapper_script() {
    print_header "Installiere Server-Wrapper"

    print_step "Kopiere start.sh..."
    cp "$DASHBOARD_DIR/start-hytale.sh" "$HYTALE_DIR/start.sh"
    chmod 755 "$HYTALE_DIR/start.sh"
    chown "$HYTALE_USER:$HYTALE_USER" "$HYTALE_DIR/start.sh"

    print_success "Wrapper-Script installiert"
}

install_update_script() {
    print_header "Installiere Update-Script"

    print_step "Kopiere hytale-update.sh..."
    cp "$DASHBOARD_DIR/hytale-update.sh" /usr/local/sbin/hytale-update.sh
    chmod 755 /usr/local/sbin/hytale-update.sh

    print_success "Update-Script installiert"
}

configure_systemd_services() {
    print_header "Konfiguriere Systemd Services"

    # Hytale Server Service
    print_step "Installiere hytale.service..."
    cp "$DASHBOARD_DIR/hytale.service" /etc/systemd/system/hytale.service

    # Dashboard Service mit Passwort
    print_step "Installiere hytale-dashboard.service..."
    cat > /etc/systemd/system/hytale-dashboard.service << EOF
[Unit]
Description=Hytale Dashboard (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$HYTALE_WEB_USER
Group=$HYTALE_WEB_USER
WorkingDirectory=$DASHBOARD_DIR
Environment=PORT=$DASHBOARD_PORT
Environment=BIND=0.0.0.0
Environment=HYTALE_SERVICE=hytale
Environment=HYTALE_DIR=$HYTALE_DIR
Environment=BACKUP_DIR=$HYTALE_DIR/backups
Environment=DASH_USER=admin
Environment=DASH_PASS=$DASH_PASSWORD
Environment=ALLOW_CONTROL=true
EOF

    # CurseForge API Key hinzufuegen wenn vorhanden
    if [ -n "$CF_API_KEY" ]; then
        echo "Environment=CF_API_KEY=$CF_API_KEY" >> /etc/systemd/system/hytale-dashboard.service
    fi

    cat >> /etc/systemd/system/hytale-dashboard.service << EOF

ExecStart=$DASHBOARD_DIR/.venv/bin/uvicorn app:app --host \${BIND} --port \${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    print_step "Lade Systemd neu..."
    systemctl daemon-reload

    print_step "Aktiviere Services..."
    systemctl enable hytale.service
    systemctl enable hytale-dashboard.service

    print_success "Systemd Services konfiguriert"
}

configure_sudoers() {
    print_header "Konfiguriere Sudo-Rechte"

    print_step "Erstelle sudoers Regeln..."
    cat > /etc/sudoers.d/hytale-dashboard << EOF
# Hytale Dashboard - Service-Steuerung
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /bin/systemctl start hytale.service
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /bin/systemctl stop hytale.service
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart hytale.service

# Backup-Script
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /usr/local/sbin/hytale-backup.sh

# Update-Script
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /usr/local/sbin/hytale-update.sh

# Systemd Override-Verzeichnis
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /bin/mkdir -p /etc/systemd/system/hytale.service.d
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/hytale.service.d/override.conf
$HYTALE_WEB_USER ALL=(ALL) NOPASSWD: /bin/systemctl daemon-reload
EOF

    chmod 440 /etc/sudoers.d/hytale-dashboard

    # Validiere sudoers
    if visudo -c -f /etc/sudoers.d/hytale-dashboard > /dev/null 2>&1; then
        print_success "Sudo-Rechte konfiguriert"
    else
        print_error "Fehler in sudoers Konfiguration!"
        rm /etc/sudoers.d/hytale-dashboard
        exit 1
    fi
}

configure_firewall() {
    print_header "Konfiguriere Firewall"

    if ! command -v ufw &> /dev/null; then
        print_warning "UFW nicht installiert, ueberspringe Firewall-Konfiguration"
        return
    fi

    print_step "Oeffne Port $HYTALE_PORT/udp (Hytale Server)..."
    ufw allow "$HYTALE_PORT/udp" comment "Hytale Server" > /dev/null 2>&1 || true

    print_step "Oeffne Port $DASHBOARD_PORT/tcp (Dashboard)..."
    ufw allow "$DASHBOARD_PORT/tcp" comment "Hytale Dashboard" > /dev/null 2>&1 || true

    # UFW aktivieren wenn noch nicht aktiv
    if ! ufw status | grep -q "Status: active"; then
        print_warning "UFW ist nicht aktiviert!"
        if ask_yes_no "UFW jetzt aktivieren?"; then
            ufw --force enable
            print_success "UFW aktiviert"
        fi
    else
        print_success "Firewall-Regeln hinzugefuegt"
    fi
}

create_default_configs() {
    print_header "Erstelle Standard-Konfigurationen"

    # Server config.json
    if [ ! -f "$HYTALE_DIR/config.json" ]; then
        print_step "Erstelle config.json..."
        cat > "$HYTALE_DIR/config.json" << 'EOF'
{
  "Version": 3,
  "ServerName": "Hytale Server",
  "MOTD": "",
  "Password": "",
  "MaxPlayers": 100,
  "MaxViewRadius": 32,
  "Defaults": {
    "World": "default",
    "GameMode": "Adventure"
  },
  "PlayerStorage": {
    "Type": "Hytale"
  },
  "AuthCredentialStore": {
    "Type": "Encrypted",
    "Path": "auth.enc"
  }
}
EOF
        chown "$HYTALE_USER:$HYTALE_USER" "$HYTALE_DIR/config.json"
        chmod 644 "$HYTALE_DIR/config.json"
    fi

    # World config.json (new path since Hytale Server 2026.01)
    if [ ! -f "$HYTALE_DIR/Server/universe/worlds/default/config.json" ]; then
        print_step "Erstelle World config.json..."
        cat > "$HYTALE_DIR/Server/universe/worlds/default/config.json" << 'EOF'
{
  "Version": 1,
  "Name": "default",
  "GameMode": "Adventure",
  "Seed": "",
  "WorldGenerator": {
    "Type": "Hytale"
  }
}
EOF
        chown "$HYTALE_USER:$HYTALE_USER" "$HYTALE_DIR/Server/universe/worlds/default/config.json"
        chmod 664 "$HYTALE_DIR/Server/universe/worlds/default/config.json"
    fi

    print_success "Konfigurationsdateien erstellt"
}

start_dashboard() {
    print_header "Starte Dashboard"

    print_step "Starte hytale-dashboard.service..."
    systemctl start hytale-dashboard.service

    sleep 2

    if systemctl is-active --quiet hytale-dashboard.service; then
        print_success "Dashboard laeuft!"
    else
        print_error "Dashboard konnte nicht gestartet werden!"
        echo "Pruefe Logs mit: journalctl -u hytale-dashboard -n 50"
    fi
}

#===============================================================================
# Abschluss-Informationen
#===============================================================================

print_completion_info() {
    local ip_addr
    ip_addr=$(hostname -I | awk '{print $1}')

    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                                                                ║${NC}"
    echo -e "${GREEN}║           INSTALLATION ERFOLGREICH ABGESCHLOSSEN!             ║${NC}"
    echo -e "${GREEN}║                                                                ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}DASHBOARD ZUGANG${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  URL:       ${GREEN}http://$ip_addr:$DASHBOARD_PORT${NC}"
    echo -e "  Benutzer:  ${GREEN}admin${NC}"
    echo -e "  Passwort:  ${GREEN}$DASH_PASSWORD${NC}"
    echo ""

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}NAECHSTE SCHRITTE - HYTALE SERVER INSTALLIEREN${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  1. Hytale Downloader herunterladen:"
    echo "     - Besuche https://hytale.com/ und lade den Server-Downloader"
    echo "     - Kopiere 'hytale-downloader-linux-amd64' nach:"
    echo -e "       ${BLUE}$HYTALE_DIR/.downloader/${NC}"
    echo ""
    echo "  2. Server herunterladen (als hytale-User):"
    echo -e "     ${BLUE}sudo -u hytale bash${NC}"
    echo -e "     ${BLUE}cd $HYTALE_DIR${NC}"
    echo -e "     ${BLUE}.downloader/hytale-downloader-linux-amd64 \\${NC}"
    echo -e "     ${BLUE}  -download-path .downloader/game.zip \\${NC}"
    echo -e "     ${BLUE}  -credentials-path .downloader/.hytale-downloader-credentials.json${NC}"
    echo ""
    echo "     (Beim ersten Start oeffnet sich ein Browser fuer OAuth)"
    echo ""
    echo "  3. Server entpacken:"
    echo -e "     ${BLUE}cd $HYTALE_DIR && unzip .downloader/game.zip${NC}"
    echo ""
    echo "  4. Server starten:"
    echo -e "     ${BLUE}sudo systemctl start hytale.service${NC}"
    echo ""

    if [ -z "$CF_API_KEY" ]; then
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${YELLOW}OPTIONAL: CURSEFORGE INTEGRATION${NC}"
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo "  Um Mods direkt aus CurseForge zu installieren:"
        echo ""
        echo "  1. Erstelle einen API Key: https://console.curseforge.com/"
        echo ""
        echo "  2. Fuege den Key zur Konfiguration hinzu:"
        echo -e "     ${BLUE}sudo systemctl edit hytale-dashboard.service${NC}"
        echo ""
        echo "     [Service]"
        echo "     Environment=CF_API_KEY=DEIN_API_KEY"
        echo ""
        echo "  3. Dashboard neu starten:"
        echo -e "     ${BLUE}sudo systemctl restart hytale-dashboard${NC}"
        echo ""
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}NUETZLICHE BEFEHLE${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  Dashboard Status:    sudo systemctl status hytale-dashboard"
    echo "  Dashboard Logs:      journalctl -u hytale-dashboard -f"
    echo "  Server Status:       sudo systemctl status hytale"
    echo "  Server Logs:         journalctl -u hytale -f"
    echo "  Server Konsole:      echo 'help' > $HYTALE_DIR/.console_pipe"
    echo ""

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}SICHERHEITSHINWEISE${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  - Verwende einen Reverse-Proxy (nginx/caddy) mit HTTPS"
    echo "  - Beschraenke Dashboard-Zugriff auf vertrauenswuerdige IPs:"
    echo -e "    ${BLUE}sudo ufw delete allow $DASHBOARD_PORT/tcp${NC}"
    echo -e "    ${BLUE}sudo ufw allow from 192.168.1.0/24 to any port $DASHBOARD_PORT proto tcp${NC}"
    echo ""
    echo -e "${GREEN}Viel Spass mit deinem Hytale Server!${NC}"
    echo ""
}

#===============================================================================
# Hauptprogramm
#===============================================================================

main() {
    clear
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║                                                                ║${NC}"
    echo -e "${CYAN}║          ${GREEN}HYTALE DASHBOARD - INSTALLATION${CYAN}                      ║${NC}"
    echo -e "${CYAN}║                                                                ║${NC}"
    echo -e "${CYAN}║          github.com/zonfacter/hytale-dashboard                 ║${NC}"
    echo -e "${CYAN}║                                                                ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Voraussetzungen pruefen
    check_root
    check_os

    echo ""
    echo -e "${YELLOW}Dieses Script wird folgendes installieren:${NC}"
    echo "  - System-Benutzer: hytale, hytale-web"
    echo "  - Verzeichnisse: /opt/hytale-server, /opt/hytale-dashboard"
    echo "  - Systemd Services: hytale.service, hytale-dashboard.service"
    echo "  - Firewall-Regeln fuer Ports $HYTALE_PORT und $DASHBOARD_PORT"
    echo ""

    if ! ask_yes_no "Installation fortsetzen?"; then
        echo "Installation abgebrochen."
        exit 0
    fi

    # Benutzereingaben sammeln
    print_header "Konfiguration"

    echo "Bitte gib ein Passwort fuer das Dashboard ein."
    echo "(Benutzer: admin)"
    echo ""
    DASH_PASSWORD=$(ask_password "Dashboard-Passwort")
    echo ""

    if ask_yes_no "Hast du einen CurseForge API Key?" "n"; then
        CF_API_KEY=$(ask_input "CurseForge API Key")
    fi

    DASHBOARD_PORT=$(ask_input "Dashboard Port" "$DASHBOARD_PORT")
    HYTALE_PORT=$(ask_input "Hytale Server Port" "$HYTALE_PORT")

    echo ""
    print_info "Starte Installation..."
    echo ""

    # Installation durchfuehren
    install_dependencies
    create_users
    setup_server_directories
    install_dashboard
    install_wrapper_script
    install_update_script
    configure_systemd_services
    configure_sudoers
    configure_firewall
    create_default_configs
    start_dashboard

    # Abschluss
    print_completion_info
}

# Script ausfuehren
main "$@"
