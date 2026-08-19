#!/usr/bin/env bash

###############################################################################
# INVENTARIO DATACENTER
###############################################################################

set -Eeuo pipefail

trap '
rc=$?
printf "[ERROR] Exit Code: %s\n" "$rc"
printf "[ERROR] Linea    : %s\n" "$LINENO"
printf "[ERROR] Comando  : %s\n" "$BASH_COMMAND"
exit "$rc"
' ERR

DEBUG="@option.DEBUG_SCRIPT@"

if [[ "$DEBUG" == "true" ]]; then
    PS4='+${LINENO}: '
    set -x
fi

OUTPUT_FORMAT="@option.OUTPUT_FORMAT@"

case "$OUTPUT_FORMAT" in
    text|raw-csv)
        ;;
    *)
        echo "[ERROR] Formato de salida no valido: $OUTPUT_FORMAT" >&2
        exit 1
        ;;
esac



###############################################################################
# Funciones auxiliares
###############################################################################

value_or_na() {
    # Normaliza el texto (elimina saltos de linea, tabulaciones y espacios 
    # extra) y devuelve "N/A" si la cadena queda vacia.
    local cleaned
    cleaned=$(printf '%s\n' "$1" | tr '\t\r\n' '   ' | sed 's/  */ /g; s/^ *//; s/ *$//')

    if [[ -z "$cleaned" ]]; then
        echo "N/A"
    else
        echo "$cleaned"
    fi
}

with_timeout() {
    # Ejecuta un comando con timeout si la herramienta 'timeout' esta disponible
    if exists timeout; then
        timeout 15 "$@"
    else
        "$@"
    fi
}

safe_capture() {
    # Ejecuta un comando, captura su stdout, y nunca dispara errexit si falla.
    # NOTA: no aplicar dentro de pipes, solamente aplicar a comandos unicos que
    # puedan propagar algun codigo de error.
    "$@" 2>/dev/null || true
}

exists() {
    # Comprueba si un comando existe (desde el PATH del usuario)
    # NOTA: no aplicar fuera de condiciones (if), o puede disparar errexit
    # en script reforzado.
    command -v "$1" >/dev/null 2>&1
}

find_command_user() {
    # Busca un comando en PATH o en rutas conocidas, verificando que sea
    # ejecutable POR EL USUARIO ACTUAL (sin sudo). Usar para comandos que el
    # script invoca directamente, sin pasar por sudo.
    local cmd="$1"
    local path
    
    # 1) Atajo rapido: PATH del usuario actual
    path=$(safe_capture command -v "$cmd")
    if [[ -n "$path" ]]; then
        echo "$path"
        return 0
    fi
    
    # 2) Rutas conocidas por FS
    local dirs="/sbin /usr/sbin /bin /usr/bin /usr/local/sbin /usr/local/bin"
    local dir
    for dir in $dirs; do
        if [[ -x "$dir/$cmd" ]]; then
            echo "$dir/$cmd"
            return 0
        fi
    done
    return 0
}

find_command_privileged() {
    # Busca un comando en rutas conocidas para uso EXCLUSIVO via sudo.
    # Primero intenta resolverlo por PATH (command -v), como atajo rapido para
    # el caso comun (binario con permisos normales, ej. 0755). Si no aparece
    # ahi, cae a una busqueda por EXISTENCIA (-f), no por -x, en rutas
    # conocidas (esto es lo que cubre el caso de binarios 0700 root-only,
    # donde el usuario Rundeck no tiene bit de ejecucion pero sudo si podra
    # ejecutarlos). La responsabilidad de que el usuario Rundeck pueda invocar
    # estos binarios recae en sudoers (NOPASSWD), no en los permisos del archivo.
    if ! exists sudo; then
        return 0
    fi
    
    local cmd="$1"
    shift
    local path
    
    # 1) Atajo rapido: PATH del usuario actual (cubre el caso 0755 comun)
    path=$(safe_capture command -v "$cmd")
    if [[ -n "$path" ]]; then
        echo "$path"
        return 0
    fi
    
    # 2) Rutas conocidas por FS: chequeo de EXISTENCIA, no de -x
    #    (permite detectar binarios 0700 root-only que solo sudo puede correr)
    local dirs="/sbin /usr/sbin /bin /usr/bin /usr/local/sbin /usr/local/bin"
    # Rutas extra especificas, si se pasan como argumentos adicionales
    local dir
    for dir in $dirs "$@"; do
        if [[ -f "$dir/$cmd" ]]; then
            echo "$dir/$cmd"
            return 0
        fi
    done
    return 0
}

###############################################################################
# VARIABLES GLOBALES
# Comandos que el usuario Rundeck no reconoce desde su PATH, por lo tanto
# debe identificar su ruta absoluta
###############################################################################

IP_CMD=$(find_command_user ip)
IFCONFIG_CMD=$(find_command_user ifconfig)
DMIDECODE_CMD=$(find_command_privileged dmidecode)
PVEVERSION_CMD=$(find_command_user pveversion)
FDISK_CMD=$(find_command_privileged fdisk)
ROUTE_CMD=$(find_command_user route)
LSBRELEASE_CMD=$(find_command_user lsb_release)

###############################################################################
# Leer archivo ubicado en /sys
###############################################################################

read_sysfs() {
    local file="$1"

    if [[ -r "$file" ]]; then
        safe_capture cat "$file"
    elif exists sudo && [[ -f "$file" ]]; then
        safe_capture sudo cat "$file"
    else
        echo ""
    fi
}

###############################################################################
# Normalizar fabricante
###############################################################################

normalize_manufacturer() {
    local manufacturer="$1"

    case "$manufacturer" in
        "Dell Inc.")
            echo "Dell"
            ;;
        "Hewlett-Packard")
            echo "HP"
            ;;
        "HPE")
            echo "HPE"
            ;;
        "LENOVO")
            echo "Lenovo"
            ;;
        *)
            echo "$manufacturer"
            ;;
    esac
}

###############################################################################
# Obtener informacion especifica via DMI (sysfs con fallback a dmidecode)
###############################################################################

get_dmi() {
    local field="$1"
    local sysfs_file=""
    local value=""

    case "$field" in
        system-uuid)
            sysfs_file="/sys/class/dmi/id/product_uuid"
            ;;
        system-manufacturer)
            sysfs_file="/sys/class/dmi/id/sys_vendor"
            ;;
        system-product-name)
            sysfs_file="/sys/class/dmi/id/product_name"
            ;;
        system-serial-number)
            sysfs_file="/sys/class/dmi/id/product_serial"
            ;;
        baseboard-product-name)
            sysfs_file="/sys/class/dmi/id/board_name"
            ;;
        bios-version)
            sysfs_file="/sys/class/dmi/id/bios_version"
            ;;
        bios-release-date)
            sysfs_file="/sys/class/dmi/id/bios_date"
            ;;
        bios-vendor)
            sysfs_file="/sys/class/dmi/id/bios_vendor"
            ;;
        *)
            return
            ;;
    esac

    # Leer archivo sysfs
    if [[ -e "$sysfs_file" ]]; then
        value=$(read_sysfs "$sysfs_file")
    fi

    # Leer salida de dmidecode
    if [[ -z "$value" && -n "$DMIDECODE_CMD" ]]; then
        value=$(safe_capture sudo "$DMIDECODE_CMD" -s "$field")
    fi

    # En caso de que $1 sea marca, normalizar
    if [[ "$field" == "system-manufacturer" ]]; then
        value=$(normalize_manufacturer "$value")
    fi

    echo "$value"
}

###############################################################################
# Deteccion tipo de maquina
###############################################################################

is_vm_signature() {
    # Recibe una cadena en minusculas y determina si contiene alguna
    # firma conocida de hipervisor/VM (manufacturer, product name, bios vendor, etc).
    local str="$1"
    case "$str" in
        *virtualbox*|*vmware*|*kvm*|*qemu*|*bochs*|*hyper-v*|*hyperv*| \
        *xen*|*domu*|*openstack*|*rhev*|*ovirt*|*seabios*|*red\ hat*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

is_hypervisor() {
    # NOTA: si el sistema no tiene el comando timeout (coreutils viejo) y 
    # virsh/xl cuelgan por un daemon no responsivo, el script podria quedarse 
    # colgado indefinidamente. Riesgo bajo en la practica.
    
    # Proxmox VE
    if [[ -n "$PVEVERSION_CMD" ]]; then
        return 0
    fi

    # VMware ESXi
    if exists vmware && with_timeout vmware -v >/dev/null 2>&1; then
        return 0
    fi

    # Esxcli
    if exists esxcli && with_timeout esxcli system version get >/dev/null 2>&1; then
        return 0
    fi

    # Xen
    if exists xl && with_timeout xl info >/dev/null 2>&1; then
        return 0
    fi

    # KVM/libvirt (conexion local explicita, no la URI por defecto)
    if exists virsh && with_timeout virsh -c qemu:///system list --all >/dev/null 2>&1; then
        return 0
    fi

    return 1
}

get_machine_type() {
    local machine_type="Dedicada"

    # Deteccion de hipervisor
    if is_hypervisor; then
        machine_type="Hipervisor"

    # Deteccion de VM/contenedor mediante systemd
    elif exists systemd-detect-virt; then
        local virt
        virt=$(safe_capture systemd-detect-virt)

        if [[ -n "$virt" && "$virt" != "none" ]]; then
            case "$virt" in
                docker|podman|container-other|lxc|lxc-libvirt|openvz|systemd-nspawn)
                    machine_type="Contenedor"
                    ;;
                *)
                    machine_type="VM"
                    ;;
            esac
        fi

    # Contenedores sin systemd
    elif [[ -f /.dockerenv || -f /run/.containerenv ]]; then
        machine_type="Contenedor"
        
    elif grep -qa '\(docker\|lxc\|container\|kubepods\)' /proc/1/cgroup 2>/dev/null; then
        machine_type="Contenedor"

    # Deteccion de VM mediante DMI (sysfs con fallback a dmidecode via get_dmi)
    elif [[ -r /sys/class/dmi/id/product_name || -n "$DMIDECODE_CMD" ]];  then

        local product_name sys_vendor bios_vendor
        product_name=$(get_dmi system-product-name | tr '[:upper:]' '[:lower:]')
        sys_vendor=$(get_dmi system-manufacturer | tr '[:upper:]' '[:lower:]')
        bios_vendor=$(get_dmi bios-vendor | tr '[:upper:]' '[:lower:]')

        if is_vm_signature "$product_name $sys_vendor $bios_vendor"; then
            machine_type="VM"
        fi

    # Ultimo recurso: flag del CPU
    elif grep -qi hypervisor /proc/cpuinfo 2>/dev/null; then
        machine_type="VM"

    fi

    echo "$machine_type"
}

###############################################################################
# Sistema Operativo
###############################################################################

normalize_os_name() {
    local name="$1"

    case "$name" in
        "Debian GNU/Linux"|"Debian")
            echo "Debian" ;;
        "Ubuntu")
            echo "Ubuntu" ;;
        "CentOS Linux"|"CentOS")
            echo "CentOS" ;;
        "CentOS Stream")
            echo "CentOS Stream" ;;
        "Red Hat Enterprise Linux Server"|"Red Hat Enterprise Linux")
            echo "RHEL" ;;
        "Red Hat Enterprise Linux Workstation")
            echo "RHEL Workstation" ;;
        "Rocky Linux")
            echo "Rocky Linux" ;;
        "AlmaLinux")
            echo "AlmaLinux" ;;
        "Oracle Linux Server"|"Oracle Linux")
            echo "Oracle Linux" ;;
        "Amazon Linux"|"Amazon Linux AMI")
            echo "Amazon Linux" ;;
        "SUSE Linux Enterprise Server"|"SUSE Linux Enterprise")
            echo "SLES" ;;
        "SUSE Linux Enterprise Desktop")
            echo "SLED" ;;
        "openSUSE")
            echo "openSUSE" ;;
        "Fedora Linux"|"Fedora")
            echo "Fedora" ;;
        "Arch Linux"|"Arch")
            echo "Arch Linux" ;;
        "Alpine Linux"|"Alpine")
            echo "Alpine Linux" ;;
        "Gentoo Linux"|"Gentoo")
            echo "Gentoo" ;;
        "Slackware Linux"|"Slackware")
            echo "Slackware" ;;
        "FreeBSD")
            echo "FreeBSD" ;;
        "OpenBSD")
            echo "OpenBSD" ;;
        "NetBSD")
            echo "NetBSD" ;;
        "Solaris"|"Oracle Solaris")
            echo "Solaris" ;;
        "AIX")
            echo "AIX" ;;
        "HP-UX")
            echo "HP-UX" ;;
        *)
            echo "$name" ;;
    esac
}

get_hypervisor_info() {
    # Detecta software/plataforma de hipervisor.
    # Devuelve "Nombre<US>Version", cadena vacia si no aplica.

    # Proxmox VE sobre Debian
    if [[ -n "$PVEVERSION_CMD" ]]; then
        local raw ver
        raw=$(safe_capture with_timeout "$PVEVERSION_CMD")
        raw=$(printf '%s\n' "$raw" | head -n1)
        ver=$(echo "$raw" | awk -F'/' '{print $2}')
        printf '%s\x1f%s' "Proxmox" "${ver:-N/A}"
        return
    fi

    # VMware ESXi
    if exists vmware; then
        local raw ver
        raw=$(safe_capture with_timeout vmware -v)
        ver=$(echo "$raw" | sed 's/^VMware ESXi //' | awk '{print $1}')

        if [[ -n "$ver" ]]; then
            printf '%s\x1f%s' "VMware ESXi" "$ver"
            return
        fi
    fi

    # Xen / XCP-ng
    if exists xe; then
        local raw ver
        raw=$(safe_capture with_timeout xe host-list params=software-version)
        ver=$(echo "$raw" | grep '^product_version' | head -n1 | awk -F': ' '{print $2}')

        if [[ -n "$ver" ]]; then
            printf '%s\x1f%s' "XCP-ng" "$ver"
            return
        fi
    fi

    # Xen standalone
    if exists xl && with_timeout xl info >/dev/null 2>&1; then
        local raw ver
        raw=$(safe_capture with_timeout xl info)
        ver=$(echo "$raw" | grep '^xen_version' | head -n1 | awk '{print $3}')

        if [[ -n "$ver" ]]; then
            printf '%s\x1f%s' "Xen" "$ver"
            return
        fi
    fi
}

get_os_release_field() {
    local key="$1"
    local file="/etc/os-release"
    local line value

    if [[ ! -r "$file" ]]; then 
        return 0
    fi

    line=$(safe_capture grep -m1 "^${key}=" "$file")
    if [[ -z "$line" ]]; then 
        return 0
    fi

    value="${line#*=}"
    # Quitar comillas simples o dobles envolventes, si existen
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"

    echo "$value"
}

parse_release_file() {
    # Parsea archivos tipo "CentOS release 6.10 (Final)" -> "CentOS<US>6.10"
    local file="$1"
    local release version name

    release=$(safe_capture sed 's/ release//; s/ *(.*)//' "$file")
    version="${release##* }"
    name="${release% $version}"

    printf '%s\x1f%s' "$name" "$version"
}

get_os_info() {
    # Detecta SO y version en un solo lugar. Devuelve "SO<US>VERSION".
    # Si el nodo es hipervisor (ej. Proxmox), agrega nombre/version del
    # hipervisor separados por coma: "Debian, Proxmox<US>11, 7.4-3"
    local so=""
    local version_so=""

    if [[ -f /etc/debian_version ]]; then
        so="Debian"
        version_so=$(safe_capture cat /etc/debian_version)
    elif [[ -f /etc/redhat-release ]]; then
        IFS=$'\x1f' read -r so version_so <<< "$(parse_release_file /etc/redhat-release)"
        so=$(normalize_os_name "$so")
    elif [[ -f /etc/centos-release ]]; then
        IFS=$'\x1f' read -r so version_so <<< "$(parse_release_file /etc/centos-release)"
        so=$(normalize_os_name "$so")
    elif [[ -n "$LSBRELEASE_CMD" ]]; then
        so=$(safe_capture "$LSBRELEASE_CMD" -si)
        version_so=$(safe_capture "$LSBRELEASE_CMD" -sr)
    elif [[ -f /etc/os-release ]]; then
        local name_raw version_id_raw version_raw
        name_raw=$(get_os_release_field "NAME")
        version_id_raw=$(get_os_release_field "VERSION_ID")
        version_raw=$(get_os_release_field "VERSION")
    
        so=$(normalize_os_name "$name_raw")
        version_so="${version_id_raw:-${version_raw:-N/A}}"
    elif [[ -f /etc/system-release ]]; then
        IFS=$'\x1f' read -r so version_so <<< "$(parse_release_file /etc/system-release)"
        so=$(normalize_os_name "$so")
    else
        so=$(safe_capture uname -s)
        version_so=$(safe_capture uname -r)
    fi

    local hv_info hv_name hv_version
    hv_info=$(get_hypervisor_info)
    if [[ -n "$hv_info" ]]; then
        hv_name="${hv_info%%$'\x1f'*}"
        hv_version="${hv_info#*$'\x1f'}"
        so="${so}, ${hv_name}"
        version_so="${version_so}, ${hv_version}"
    fi

    printf '%s\x1f%s' "$so" "$version_so"
}

get_host_os_name() {
    local info="$1"
    local name="${info%%$'\x1f'*}"
    echo "${name%%,*}"
}

get_host_os_version() {
    local info="$1"
    local version="${info#*$'\x1f'}"
    echo "${version%%,*}"
}

get_hypervisor_os_name() {
    local info="$1"
    local name="${info%%$'\x1f'*}"

    if [[ "$name" == *,* ]]; then
        echo "${name#*, }"
    fi
}

get_hypervisor_os_version() {
    local info="$1"
    local version="${info#*$'\x1f'}"

    if [[ "$version" == *,* ]]; then
        echo "${version#*, }"
    fi
}

###############################################################################
# Modelo CPU
###############################################################################

get_cpu_model() {
    local model=""
    if exists lscpu; then
        model=$(LC_ALL=C lscpu 2>/dev/null | awk -F': *' '/^Model name/ {print $2; exit}') || true
    fi
    if [[ -z "$model" && -r /proc/cpuinfo ]]; then
        model=$(safe_capture awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo)
    fi
    echo "$model"
}

###############################################################################
# Cores CPU
###############################################################################

get_cpu_cores() {
    local cores=""
    if exists lscpu; then
        cores=$(
            LC_ALL=C lscpu 2>/dev/null | awk -F': *' '
                /^Core\(s\) per socket/          { cores=$2 }
                /^(Socket\(s\)|CPU socket\(s\))/ { sockets=$2 }
                END {
                    if (cores != "" && sockets != "")
                        print cores * sockets
                }
            '
        ) || true
    fi
    if [[ -z "$cores" && -r /proc/cpuinfo ]]; then
        cores=$(
            safe_capture awk '
                /^physical id/ { socket=$NF }
                /^core id/     { mapa[socket ":" $NF]=1 }
                END {
                    total=0
                    for (i in mapa) total++
                    print total
                }
            ' /proc/cpuinfo
        )
        if [[ -z "$cores" || "$cores" -le 0 ]]; then
            cores=$(safe_capture grep -c '^processor' /proc/cpuinfo)
        fi
    fi
    echo "$cores"
}

###############################################################################
# Threads CPU
###############################################################################

get_cpu_threads() {
    local threads=""
    if exists lscpu; then
        threads=$(
            LC_ALL=C lscpu 2>/dev/null | awk -F': *' '
                /^CPU\(s\)/ { print $2; exit }
            '
        ) || true
    fi
    if [[ -z "$threads" && -r /proc/cpuinfo ]]; then
        threads=$(safe_capture grep -c '^processor' /proc/cpuinfo)
    fi
    echo "$threads"
}

###############################################################################
# Sockets CPU
###############################################################################

get_cpu_sockets() {
    local sockets=""
    if exists lscpu; then
        sockets=$(
            LC_ALL=C lscpu 2>/dev/null | awk -F': *' '
                /^(Socket\(s\)|CPU socket\(s\))/ { print $2; exit }
            '
        ) || true
    fi
    if [[ -z "$sockets" && -r /proc/cpuinfo ]]; then
        sockets=$(awk -F': ' '/^physical id/ {print $2}' /proc/cpuinfo 2>/dev/null | sort -u | wc -l) || true
        if [[ -z "$sockets" || "$sockets" -le 0 ]]; then
            sockets=1
        fi
    fi
    echo "$sockets"
}

###############################################################################
# Memoria
###############################################################################

get_ram_gb() {
    if [[ -r /proc/meminfo ]]; then
        safe_capture awk '
            /^MemTotal:/ {
                printf "%.1f\n", $2 / 1024 / 1024
            }
        ' /proc/meminfo
    fi
}

###############################################################################
# Determina si un dispositivo de /sys/block corresponde a un disco fisico/virtual
# valido (excluye CD-ROM, RAID software, LVM, loop, RAM, etc.)
###############################################################################

is_real_disk() {
    local d="$1"
    local dev
    dev=$(basename "$d")

    # Descartar dispositivos virtuales conocidos por nombre
    case "$dev" in
        loop*|ram*|fd*|zram*|sr*|md*|dm-*|nbd*|rbd*|nvme*c*n*)
            return 1
            ;;
    esac

    # Ignorar particiones (por si se pasa /sys/block/nvme0n1p1 o sda1)
    if [[ -f "$d/partition" ]]; then
        return 1
    fi

    # Si el dispositivo tiene sub-dispositivos (slaves), es virtual (RAID, LVM, Multipath, etc.)
    if [[ -d "$d/slaves" && "$(safe_capture ls -A "$d/slaves")" ]]; then
        return 1
    fi

    # Ignorar dispositivos de tamano 0 (lectores vacios, interfaces sin disco)
    if [[ -f "$d/size" ]]; then
        local size
        size=$(safe_capture cat "$d/size")
        if [[ "${size:-0}" -eq 0 ]]; then
            return 1
        fi
    fi

    # Ignorar CD/DVD u otros tipos SCSI no-disco (Tipo 5 = CD-ROM)
    if [[ -r "$d/device/type" ]]; then
        local type
        type=$(safe_capture cat "$d/device/type")
        if [[ "$type" = "5" ]]; then
            return 1
        fi
    fi

    return 0
}

###############################################################################
# Obtiene el tamano (en bytes) de todos los discos fisicos/validos
###############################################################################

get_disk_sizes_bytes() {

    if [[ -d /sys/block ]]; then

        local d
        local sectors

        for d in /sys/block/*; do

            [[ -d "$d" ]] || continue

            is_real_disk "$d" || continue

            [[ -r "$d/size" ]] || continue

            sectors=$(safe_capture cat "$d/size")

            case "$sectors" in
                ''|*[!0-9]*)
                    continue
                    ;;
            esac

            echo $(( sectors * 512 ))

        done

    elif [[ -n "$FDISK_CMD" ]]; then

        LC_ALL=C sudo "$FDISK_CMD" -l 2>/dev/null |
        awk '
            /^Disk \/dev\// {

                if ($2 ~ /(loop|ram|sr|fd)/)
                    next

                for (i=1; i<=NF; i++) {
                    if ($i == "bytes") {
                        print $(i-1)
                        break
                    }
                }
            }
        ' || true

    fi
}

###############################################################################
# Capacidad total de discos (GB)
###############################################################################

get_disk_capacity_gb() {

    get_disk_sizes_bytes |
    awk '
    {
        if ($1 ~ /^[0-9]+$/)
            total += $1
    }

    END {
        printf "%.2f", total / 1024 / 1024 / 1024
    }' || true
}

###############################################################################
# Resumen de discos
###############################################################################

get_disk_layout() {

    get_disk_sizes_bytes |
    sort -n |
    uniq -c |
    awk '
    function human(bytes, gb) {

        gb = bytes / 1024 / 1024 / 1024

        if (gb >= 1024)
            return sprintf("%.2f TB", gb / 1024)

        return sprintf("%.0f GB", gb)
    }

    {
        if (NR > 1)
            printf " + "

        if ($1 > 1)
            printf "%d x %s", $1, human($2)
        else
            printf "%s", human($2)
    }

    END {
        if (NR)
            printf "\n"
    }' || true
}

###############################################################################
# Agrupa niveles de RAID repetidos en un resumen legible
# Ej: entrada "RAID1\nRAID1\nRAID5" -> salida "RAID1 x2 + RAID5"
###############################################################################
format_raid_list() {
    sort | uniq -c | awk '
        {
            if (NR > 1) printf " + "
            if ($1 > 1) printf "%s x%d", $2, $1
            else printf "%s", $2
        }
        END { if (NR) printf "\n" }
    ' || true
}

###############################################################################
# RAID por hardware: Dell / LSI (perccli, megacli)
###############################################################################
get_raid_dell() {
    local c bin=""
    for c in perccli64 perccli PercCli64 megacli64 MegaCli64 megacli MegaCli; do
        bin=$(find_command_privileged "$c" \
            /opt/MegaRAID/perccli /opt/MegaRAID/MegaCli \
            /opt/lsi/perccli /opt/lsi/storcli)
        if [[ -n "$bin" ]]; then
            break
        fi
    done
    if [[ -z "$bin" ]]; then return 0; fi
    local -a cmd=(sudo "$bin")
    local output
    case "$(basename "$bin")" in
        [Pp]erc[Cc]li*|[Ss]tor[Cc][Ll][Ii]*)
            # Formato esperado (tabla): "0/0   RAID1  Optl  RW ..."
            output=$(safe_capture with_timeout "${cmd[@]}" /call/vall show)
            printf '%s\n' "$output" |
                awk '/^[0-9]+\/[0-9]+[[:space:]]+RAID/ { print $2 }' || true
            ;;
        *[Mm]ega[Cc]li*)
            # Formato esperado:
            # "RAID Level          : Primary-5, Secondary-0, ..."
            output=$(safe_capture with_timeout "${cmd[@]}" -LDInfo -Lall -aALL)
            printf '%s\n' "$output" |
                awk -F'[:,]' '
                    /RAID Level/ {
                        for (i = 1; i <= NF; i++) {
                            if ($i ~ /Primary-/) {
                                n = $i
                                sub(/.*Primary-/, "", n)
                                gsub(/[[:space:]]/, "", n)
                                print "RAID" n
                            }
                        }
                    }
                ' || true
            ;;
    esac
}

###############################################################################
# RAID por hardware: HP / HPE (ssacli, hpssacli, hpacucli)
###############################################################################
get_raid_hp() {
    local bin=""
    local c
    for c in ssacli hpssacli hpacucli; do
        bin=$(find_command_privileged "$c")
        if [[ -n "$bin" ]]; then
            break
        fi
    done
    if [[ -z "$bin" ]]; then return 0; fi
    local -a cmd=(sudo "$bin")
    local output
    # Formato esperado: "      Fault Tolerance: RAID 1"
    output=$(safe_capture with_timeout "${cmd[@]}" ctrl all show config)
    printf '%s\n' "$output" |
        awk -F': *' '
            /Fault Tolerance/ {
                level = $2
                gsub(/[[:space:]]/, "", level)
                print level
            }
        ' || true
}

###############################################################################
# RAID por hardware: Adaptec (arcconf) - comun en algunos Lenovo/Supermicro
###############################################################################
get_raid_adaptec() {
    local bin
    bin=$(find_command_privileged arcconf)
    if [[ -z "$bin" ]]; then
        return 0
    fi
    local -a cmd=(sudo "$bin")
    local output
    # Formato esperado: "RAID level                              : 5"
    # Se asume controlador 1; en equipos con multiples controladoras
    # convendria iterar, pero no es el caso comun.
    output=$(safe_capture with_timeout "${cmd[@]}" getconfig 1 ld)
    printf '%s\n' "$output" |
        awk -F': *' '
            /RAID level/ {
                level = $2
                gsub(/[[:space:]]/, "", level)
                print "RAID" level
            }
        ' || true
}

###############################################################################
# RAID por software: mdadm (via /proc/mdstat)
# Importante para sistemas viejos sin controladora dedicada y para
# hipervisores/dedicados que usan RAID por software.
###############################################################################
get_raid_mdadm() {
    if [[ ! -r /proc/mdstat ]]; then
        return 0
    fi

    safe_capture awk '
        /^md[0-9]+/ {
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^raid[0-9]+$/) {
                    level = $i
                    gsub(/raid/, "RAID", level)
                    print level
                    break
                }
            }
        }
    ' /proc/mdstat
}

###############################################################################
# Busca firmas de controladoras RAID conocidas en vendor/model de los discos
# visibles en /sys/block (los rellena el firmware de la controladora, no el
# disco fisico real, por lo que delatan la presencia de RAID por hardware
# incluso cuando el SO ve un unico disco logico).
###############################################################################
get_raid_controller_signature() {
    if [[ ! -d /sys/block ]]; then
        return 0
    fi

    local d vendor model combined

    for d in /sys/block/*; do
        [[ -d "$d" ]] || continue
        is_real_disk "$d" || continue

        vendor=""
        model=""
        if [[ -r "$d/device/vendor" ]]; then
            vendor=$(safe_capture cat "$d/device/vendor")
        fi
        if [[ -r "$d/device/model" ]]; then
            model=$(safe_capture cat "$d/device/model")
        fi

        combined="$vendor $model"
        # Recortar espacios sobrantes (los datos SCSI INQUIRY vienen
        # rellenados con espacios de padding, ej: "DELL    ")
        combined="${combined#"${combined%%[![:space:]]*}"}"
        combined="${combined%"${combined##*[![:space:]]}"}"

        case "$(echo "$combined" | tr '[:upper:]' '[:lower:]')" in
            *perc*|*megaraid*|*"smart array"*|*smartarray*| \
            *adaptec*|*"logical volume"*|*"virtual disk"*|*"raid controller"*)
                echo "$combined"
                return
                ;;
        esac
    done
}

###############################################################################
# Fallback: sin herramienta RAID detectada.
# - Si se detecta firma de controladora RAID (via vendor/model), hay RAID
#   pero no se pudo determinar el nivel -> "Desconocido (controlador X)".
# - Si no hay firma y solo hay un disco fisico, es razonable asumir sin RAID.
# - Si no hay firma pero hay multiples discos, no hay forma confiable de
#   saberlo -> se deja vacio (el llamador lo mostrara como N/A).
###############################################################################
get_raid_fallback() {
    local sig n

    sig=$(get_raid_controller_signature)
    if [[ -n "$sig" ]]; then
        echo "Desconocido (controlador $sig)"
        return
    fi

    n=$(get_disk_sizes_bytes 2>/dev/null | wc -l) || true
    if [[ "$n" -le 1 ]]; then
        echo "Sin RAID"
    fi
}

###############################################################################
# Orquestador: prueba cada proveedor en orden hasta obtener resultado
###############################################################################
get_raid_info() {
    local raw=""

    raw=$(get_raid_dell)
    if [[ -z "$raw" ]]; then
        raw=$(get_raid_hp)
    fi
    if [[ -z "$raw" ]]; then
        raw=$(get_raid_adaptec)
    fi
    if [[ -z "$raw" ]]; then
        raw=$(get_raid_mdadm)
    fi

    if [[ -n "$raw" ]]; then
        echo "$raw" | format_raid_list
    else
        get_raid_fallback
    fi
}


###############################################################################
# Conversion IP <-> entero (para calculos de red/mascara)
# El prefijo 10# fuerza base decimal en cada octeto, previniendo que bash
# interprete octetos con ceros a la izquierda (ej. 010) como octal.
###############################################################################
ip_to_int() {
    local ip="$1"
    local a b c d
    IFS='.' read -r a b c d <<< "$ip"
    echo $(( (10#$a * 16777216) + (10#$b * 65536) + (10#$c * 256) + 10#$d ))
}

int_to_ip() {
    local int="$1"
    echo "$(( (int >> 24) & 255 )).$(( (int >> 16) & 255 )).$(( (int >> 8) & 255 )).$(( int & 255 ))"
}

###############################################################################
# Cuenta bits en 1 de una mascara dotted-decimal (ej: 255.255.255.0 -> 24)
###############################################################################
netmask_to_prefix() {
    local mask="$1"
    local mint bits=0 i

    mint=$(ip_to_int "$mask")
    for ((i = 31; i >= 0; i--)); do
        (( (mint >> i) & 1 )) && bits=$((bits + 1))
    done
    echo "$bits"
}

###############################################################################
# Determina si una interfaz de red es "real" para efectos de inventario.
# Se excluyen interfaces sinteticas/efimeras que no aportan valor en un
# inventario de dataceter: loopback, veth (containers), docker0/br-*
# (bridges de Docker), virbr* (bridge NAT interno de libvirt, no es un
# uplink real), tun/tap, vnet* (interfaces vnetXX por VM en KVM/libvirt),
# fwbr*/fwln*/fwpr* (bridges de firewall por-VM que genera Proxmox).
# bond* y vmbr* son interfaces "reales" desde el punto de vista de inventario 
# (bond = NIC logica por LACP/teaming; vmbr = bridge de uplink real en Proxmox,
# analogo a un bridge de red fisica). Notar que las NICs fisicas
# "esclavas" de un bond (ej. eth0/eth1 dentro de bond0) tambien apareceran
# en el listado, normalmente sin IP propia (la IP vive en el bond) - esto
# es intencional: documenta el cableado fisico real del nodo.
###############################################################################
is_real_nic() {
    local dev="$1"
    case "$dev" in
        lo|sit*|veth*|docker*|br-*|virbr*|tun*|tap*|vnet*|fwbr*|fwln*|fwpr*)
            return 1
            ;;
    esac
    return 0
}

###############################################################################
# Lee el operstate (up/down/unknown) de una interfaz desde sysfs.
# Si no esta disponible, devuelve "unknown".
###############################################################################
get_nic_state() {
    local dev="$1"
    local state="unknown"
    
    if [[ -r "/sys/class/net/$dev/operstate" ]]; then
        state=$(safe_capture cat "/sys/class/net/$dev/operstate")
    elif [[ -n "$IFCONFIG_CMD" ]]; then
        if "$IFCONFIG_CMD" "$dev" 2>/dev/null | grep -q 'RUNNING'; then
            state="up"
        else
            state="down"
        fi
    fi

    echo "${state:-unknown}"
}

###############################################################################
# Enumera los nombres de interfaces de red "reales" (ver is_real_nic),
# una por linea. Prueba /sys/class/net -> ip -> ifconfig, en ese orden.
# Funcion base compartida por get_primary_interface y get_network_interfaces.
###############################################################################
list_real_nics() {
    local dev d

    if [[ -d /sys/class/net ]]; then
        for d in /sys/class/net/*; do
            [[ -d "$d" ]] || continue
            dev=$(basename "$d")
            if is_real_nic "$dev"; then
                echo "$dev"
            fi
        done

    elif [[ -n "$IP_CMD" ]]; then
        while IFS= read -r dev; do
           if is_real_nic "$dev"; then
                echo "$dev"
            fi
        done < <("$IP_CMD" -o link show 2>/dev/null |
            awk -F': ' '{print $2}' || true)

    elif [[ -n "$IFCONFIG_CMD" ]]; then
        while IFS= read -r dev; do
            if is_real_nic "$dev"; then
                echo "$dev"
            fi
        done < <("$IFCONFIG_CMD" -a 2>/dev/null |
            awk '/^[a-zA-Z0-9]/ { sub(/:$/,"",$1); print $1 }' || true)
    fi
}


###############################################################################
# Interfaz principal: la que tiene la ruta por defecto.
# Prueba ip route -> route -n -> /proc/net/route (universal, sin
# dependencias externas) -> primera interfaz real que este UP.
# NOTA: SIN USO DIRECTO EN ESTE SCRIPT. SIRVE PARA MAS ADELANTE.
###############################################################################
get_primary_interface() {
    local iface=""

    if [[ -n "$IP_CMD" ]]; then
        iface=$("$IP_CMD" route show default 2>/dev/null |
            awk '/^default/ { for (i=1;i<=NF;i++) if ($i=="dev") print $(i+1) }' |
            head -n1) || true
    fi

    if [[ -z "$iface" && -n "$ROUTE_CMD" ]]; then
        iface=$("$ROUTE_CMD" -n 2>/dev/null | awk '$1=="0.0.0.0" {print $NF; exit}') || true
    fi

    if [[ -z "$iface" && -r /proc/net/route ]]; then
        iface=$(safe_capture awk '$2=="00000000" {print $1; exit}' /proc/net/route)
    fi

    if [[ -z "$iface" ]]; then
        local dev
        while IFS= read -r dev; do
            if [[ "$(get_nic_state "$dev")" == "up" ]]; then
                iface="$dev"
                break
            fi
        done < <(list_real_nics)
    fi

    echo "$iface"
}

###############################################################################
# Obtiene IP y prefijo de una interfaz en formato "ip/prefix" (ej: 10.0.0.5/24).
# Funcion base interna: prueba 'ip', luego 'ifconfig' (formato viejo y nuevo).
# Es la unica funcion que sabe parsear la salida de ip/ifconfig; get_ip_address
# y get_network_cidr solo derivan sobre este resultado.
###############################################################################
get_ip_cidr_raw() {
    local iface="$1"
    if [[ -z "$iface" ]]; then return 0; fi
    

    if [[ -n "$IP_CMD" ]]; then
        local out
        out=$("$IP_CMD" -4 -o addr show dev "$iface" 2>/dev/null |
            awk '{print $4; exit}') || true
        if [[ -n "$out" ]]; then
            echo "$out"
            return 0
        fi
    fi

    if [[ -n "$IFCONFIG_CMD" ]]; then
        local raw ip netmask prefix
        raw=$(safe_capture "$IFCONFIG_CMD" "$iface")

        ip=$(printf '%s\n' "$raw" | awk '
            /inet addr:/ { for (i=1;i<=NF;i++) if ($i ~ /^addr:/) { sub(/^addr:/,"",$i); print $i; exit } }
            /inet / && !/inet6/ { for (i=1;i<=NF;i++) if ($i=="inet") { print $(i+1); exit } }
        ') || true
        if [[ -z "$ip" ]]; then return 0; fi

        netmask=$(printf '%s\n' "$raw" | awk '
            /Mask:/    { for (i=1;i<=NF;i++) if ($i ~ /^Mask:/) { sub(/^Mask:/,"",$i); print $i; exit } }
            /netmask/  { for (i=1;i<=NF;i++) if ($i=="netmask") { print $(i+1); exit } }
        ') || true

        if [[ -n "$netmask" ]]; then
            prefix=$(safe_capture netmask_to_prefix "$netmask")
            if [[ -n "$prefix" ]]; then
                echo "${ip}/${prefix}"
            fi
        fi
    fi
}

###############################################################################
# Valida que un prefijo sea un entero entre 0 y 32
###############################################################################
is_valid_prefix() {
    local prefix="$1"
    case "$prefix" in
        ''|*[!0-9]*) return 1 ;;
    esac
    (( prefix >= 0 && prefix <= 32 ))
}

###############################################################################
# IP (IPv4) de una interfaz (por defecto, la principal)
###############################################################################
get_ip_address() {
    local iface="${1:-$(get_primary_interface)}"
    local raw
    raw=$(get_ip_cidr_raw "$iface")
    if [[ -z "$raw" ]]; then return 0; fi

    echo "${raw%/*}"
}

###############################################################################
# Red IP en formato CIDR (direccion de red, no la IP del host): x.x.x.x/YY
###############################################################################
get_network_cidr() {
    local iface="${1:-$(get_primary_interface)}"
    local raw ip prefix
    raw=$(get_ip_cidr_raw "$iface")
    if [[ -z "$raw" || "$raw" != */* ]]; then return 0; fi
    ip="${raw%/*}"
    prefix="${raw#*/}"
    if ! is_valid_prefix "$prefix"; then
        return 0
    fi
    local ip_int network_int
    ip_int=$(ip_to_int "$ip")
    # Se calcula mask_int via awk para evitar el operador << dentro de $((...)),
    # que confunde resaltadores de sintaxis (falso positivo de heredoc).
    # La formula es equivalente a: (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    local mask_int
    mask_int=$(awk -v p="$prefix" 'BEGIN {
        mask = 0
        for (i = 0; i < p; i++)
            mask = mask * 2 + 1
        # mask tiene p bits en 1 desde el bit menos significativo;
        # desplazarlo (32-p) posiciones hacia la izquierda via multiplicacion
        shift = 1
        for (i = 0; i < 32 - p; i++)
            shift *= 2
        printf "%d\n", (mask * shift) % 4294967296
    }')
    network_int=$(( ip_int & mask_int ))
    echo "$(int_to_ip "$network_int")/$prefix"
}

###############################################################################
# Direccion MAC de una interfaz (por defecto, la principal)
###############################################################################
get_mac_address() {
    local iface="${1:-$(get_primary_interface)}"
    if [[ -z "$iface" ]]; then return 0; fi

    local mac=""

    if [[ -r "/sys/class/net/$iface/address" ]]; then
        mac=$(safe_capture cat "/sys/class/net/$iface/address")
    fi

    if [[ -z "$mac" && -n "$IP_CMD" ]]; then
        mac=$("$IP_CMD" link show "$iface" 2>/dev/null |
            awk '/link\/ether/ { print $2; exit }') || true
    fi

    if [[ -z "$mac" && -n "$IFCONFIG_CMD" ]]; then
        mac=$("$IFCONFIG_CMD" "$iface" 2>/dev/null | awk '
            /HWaddr/ { for (i=1;i<=NF;i++) if ($i=="HWaddr") { print $(i+1); exit } }
            /ether/  { for (i=1;i<=NF;i++) if ($i=="ether")  { print $(i+1); exit } }
        ') || true
    fi
    
    echo "$mac"
}

###############################################################################
# Recorre todas las interfaces "reales" (ver is_real_nic) y arma 5 listas
# alineadas por indice, separadas por ", ": INTERFACES, INTERFACES_ESTADO,
# IP, RED_IP, MAC.
#
# Ej. si el nodo tiene eth0 (up) y eth1 (down):
#   INTERFACES        = "eth0, eth1"
#   INTERFACES_ESTADO = "up, down"
#   IP                = "10.0.0.5, N/A"
#   RED_IP            = "10.0.0.0/24, N/A"
#   MAC               = "ab:cd:ef:12:34:56, 98:76:54:32:ab:cd"
#
# Una interfaz sin IP configurada (ej. NIC esclava de un bond, o una
# interfaz down) aparece igual en INTERFACES/INTERFACES_ESTADO/MAC, pero
# con "N/A" en su posicion de IP/RED_IP - esto es intencional para
# mantener los 5 indices alineados entre si.
#
# Devuelve las 5 listas separadas por \x1f (no usar ", " como separador
# aqui, ya que las listas individuales YA contienen ", ").
###############################################################################
get_network_table() {
    local dev state ip cidr mac
    local list_ifaces="" list_states="" list_ips="" list_cidrs="" list_macs=""

    while IFS= read -r dev; do
        [[ -z "$dev" ]] && continue

        state=$(get_nic_state "$dev")
        ip=$(get_ip_address "$dev")
        cidr=$(get_network_cidr "$dev")
        mac=$(get_mac_address "$dev")

        if [[ -n "$list_ifaces" ]]; then
            list_ifaces="${list_ifaces}, "
            list_states="${list_states}, "
            list_ips="${list_ips}, "
            list_cidrs="${list_cidrs}, "
            list_macs="${list_macs}, "
        fi

        list_ifaces="${list_ifaces}${dev}"
        list_states="${list_states}${state:-unknown}"
        list_ips="${list_ips}${ip:-N/A}"
        list_cidrs="${list_cidrs}${cidr:-N/A}"
        list_macs="${list_macs}${mac:-N/A}"

    done < <(list_real_nics)

    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s' \
        "$list_ifaces" "$list_states" "$list_ips" "$list_cidrs" "$list_macs"
}

###############################################################################
# FQDN del equipo
###############################################################################
get_fqdn() {
    local fqdn="" short domain

    if exists hostname; then
        fqdn=$(safe_capture hostname -f)
    fi

    if [[ -z "$fqdn" ]]; then
        short=$(safe_capture hostname)

        if exists getent; then
            fqdn=$(getent hosts "$short" 2>/dev/null | awk '{print $2; exit}') || true
        fi

        if [[ -z "$fqdn" && -r /etc/resolv.conf ]]; then
            domain=$(safe_capture awk '/^domain/ {print $2; exit} /^search/ {print $2; exit}' /etc/resolv.conf)
            if [[ -n "$domain" && -n "$short" ]]; then
                fqdn="${short}.${domain}"
            fi
        fi

        if [[ -z "$fqdn" ]]; then
            fqdn="$short"
        fi
    fi

    echo "$fqdn"
}


###############################################################################
# Nombre de maquina
###############################################################################

NOMBRE_MAQUINA=$(safe_capture hostname)


###############################################################################
# Deteccion tipo de maquina
###############################################################################

TIPO_MAQUINA=$(get_machine_type)


###############################################################################
# UUID
###############################################################################

UUID=$(get_dmi system-uuid)


###############################################################################
# Sistema operativo
###############################################################################

SO_INFO=$(get_os_info)

SO=$(get_host_os_name "$SO_INFO")
VERSION_SO=$(get_host_os_version "$SO_INFO")
SO_HIPERVISOR=$(get_hypervisor_os_name "$SO_INFO")
VERSION_SO_HIPERVISOR=$(get_hypervisor_os_version "$SO_INFO")

###############################################################################
# Kernel
###############################################################################

KERNEL=$(uname -r)


###############################################################################
# Informacion especifica de hardware
###############################################################################

MARCA=""
MODELO=""
PN=""
SERIAL=""
SERVICE_TAG=""

if [[ "$TIPO_MAQUINA" == "Hipervisor" || "$TIPO_MAQUINA" == "Dedicada" ]]; then
    MARCA=$(get_dmi system-manufacturer)
    MODELO=$(get_dmi system-product-name)
    SERIAL=$(get_dmi system-serial-number)

    if [[ "$MARCA" == *Dell* ]]; then
        SERVICE_TAG="$SERIAL"
        SERIAL=""
    else
        PN=$(get_dmi baseboard-product-name)
    fi
fi

###############################################################################
# BIOS
###############################################################################

BIOS_VERSION=$(get_dmi bios-version)
BIOS_DATE=$(get_dmi bios-release-date)

###############################################################################
# CPU
###############################################################################

CPU_MODELO=$(get_cpu_model)
CPU_CORES=$(get_cpu_cores)
CPU_THREADS=$(get_cpu_threads)
CPU_SOCKETS=$(get_cpu_sockets)

###############################################################################
# Memoria
###############################################################################

RAM_GB=$(get_ram_gb)

###############################################################################
# Almacenamiento
###############################################################################

ALM_CAPACIDAD=$(get_disk_capacity_gb)

ALM_DISCOS=""

if [[ "$TIPO_MAQUINA" == "VM" ]]; then
    ALM_DISCOS=$(get_disk_layout)
fi

###############################################################################
# RAID
###############################################################################

RAID=""
if [[ "$TIPO_MAQUINA" == "Hipervisor" || "$TIPO_MAQUINA" == "Dedicada" ]]; then
    RAID=$(get_raid_info)
fi


###############################################################################
# Red
###############################################################################

NET_TABLE=$(get_network_table)
IFS=$'\x1f' read -r INTERFACES INTERFACES_ESTADO IP RED_IP MAC <<< "$NET_TABLE"

FQDN=$(get_fqdn)




###############################################################################
# Datos de salida (deben respetar posicion 1 a 1 entre OUTPUT_KEYS y OUTPUT_VALUES)
###############################################################################

OUTPUT_KEYS=(
    "Nombre maquina"
    "Tipo de maquina"
    "SO Host"
    "Version SO Host"
    "SO Hipervisor"
    "Version SO Hipervisor"
    "Kernel"
    "FQDN"
    "Interfaces"
    "Interfaces estado"
    "IP"
    "Red IP"
    "MAC"
    "CPU Modelo"
    "Cores"
    "Threads"
    "Sockets"
    "RAM (GB)"
    "Discos"
    "Capacidad visible (GB)"
    "RAID"
    "Marca"
    "Modelo"
    "P/N"
    "Serial Number"
    "Service Tag"
    "UUID"
    "Version BIOS"
    "Fecha BIOS"
)

OUTPUT_VALUES=(
    "$NOMBRE_MAQUINA"
    "$TIPO_MAQUINA"
    "$SO"
    "$VERSION_SO"
    "$SO_HIPERVISOR"
    "$VERSION_SO_HIPERVISOR"
    "$KERNEL"
    "$FQDN"
    "$INTERFACES"
    "$INTERFACES_ESTADO"
    "$IP"
    "$RED_IP"
    "$MAC"
    "$CPU_MODELO"
    "$CPU_CORES"
    "$CPU_THREADS"
    "$CPU_SOCKETS"
    "$RAM_GB"
    "$ALM_DISCOS"
    "$ALM_CAPACIDAD"
    "$RAID"
    "$MARCA"
    "$MODELO"
    "$PN"
    "$SERIAL"
    "$SERVICE_TAG"
    "$UUID"
    "$BIOS_VERSION"
    "$BIOS_DATE"
)


###############################################################################
# Imprime un valor para la salida clave-valor.
# El valor queda entre comillas dobles.
# Las comillas dobles internas se escapan duplicandolas.
###############################################################################

key_value_field() {
    local value="$1"

    value=${value//\"/\"\"}

    printf '"%s"' "$value"
}


###############################################################################
# Salida clave-valor
###############################################################################

print_key_value_output() {
    local i
    local key
    local value

    i=0

    while [[ $i -lt ${#OUTPUT_KEYS[@]} ]]; do

        key="${OUTPUT_KEYS[$i]}"
        value="${OUTPUT_VALUES[$i]}"

        value=$(value_or_na "$value")

        if [[ "$OUTPUT_FORMAT" == "text" ]]; then

            printf '%-24s : %s\n' "$key" "$value"

        else

            printf '%s=' "$key"
            key_value_field "$value"

            if [[ $((i + 1)) -lt ${#OUTPUT_KEYS[@]} ]]; then
                printf ','
            else
                printf '\n'
            fi

        fi

        i=$((i + 1))
    done
}

###############################################################################
# Salida
###############################################################################

print_key_value_output

