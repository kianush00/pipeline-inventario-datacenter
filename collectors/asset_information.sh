#!/usr/bin/env bash

###############################################################################
# DATACENTER INVENTORY
###############################################################################

set -Eeuo pipefail

export LC_ALL=C
export LANG=C

# shellcheck disable=SC2154
trap '
rc=$?
printf "[ERROR] Exit Code: %s\n" "$rc"
printf "[ERROR] Line     : %s\n" "$LINENO"
printf "[ERROR] Command  : %s\n" "$BASH_COMMAND"
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
        echo "[ERROR] Invalid output format: $OUTPUT_FORMAT" >&2
        exit 1
        ;;
esac



###############################################################################
# Helper functions
###############################################################################

value_or_na() {
    # Normalizes the text (removes line breaks, tabs and extra
    # spaces) and returns "N/A" if the string ends up empty.
    local cleaned
    cleaned=$(printf '%s\n' "$1" | tr '\t\r\n' '   ' | sed 's/  */ /g; s/^ *//; s/ *$//')

    if [[ -z "$cleaned" ]]; then
        echo "N/A"
    else
        echo "$cleaned"
    fi
}

is_positive_int() {
    # Checks whether the value is a strictly positive integer.
    # NOTE: do not use outside conditions (if), or it may trigger errexit
    # in a hardened script.
    [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 > 0 ))
}

with_timeout() {
    # Runs a command with a timeout if the 'timeout' tool is available
    if exists timeout; then
        timeout 15 "$@"
    else
        "$@"
    fi
}

safe_capture() {
    # Runs a command, captures its stdout, and never triggers errexit if it fails.
    # NOTE: do not use inside pipes, only apply to single commands that
    # may propagate some error code.
    "$@" 2>/dev/null || true
}

exists() {
    # Checks whether a command exists (from the user's PATH)
    # NOTE: do not use outside conditions (if), or it may trigger errexit
    # in a hardened script.
    command -v "$1" >/dev/null 2>&1
}

find_command_user() {
    # Looks for a command in PATH or in known paths, verifying that it is
    # executable BY THE CURRENT USER (without sudo). Use for commands that the
    # script invokes directly, without going through sudo.
    local cmd="$1"
    local path
    
    # 1) Quick shortcut: PATH of the current user
    path=$(safe_capture command -v "$cmd")
    if [[ -n "$path" ]]; then
        echo "$path"
        return 0
    fi
    
    # 2) Known FS paths
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
    # Looks for a command in known paths for EXCLUSIVE use via sudo.
    # It first tries to resolve it through PATH (command -v), as a quick shortcut for
    # the common case (binary with regular permissions, e.g. 0755). If it does not show up
    # there, it falls back to a search by EXISTENCE (-f), not by -x, in known
    # paths (this is what covers the case of 0700 root-only binaries,
    # where the Rundeck user has no execute bit but sudo will be able to
    # run them). The responsibility for the Rundeck user being able to invoke
    # these binaries lies with sudoers (NOPASSWD), not with the file permissions.
    if ! exists sudo; then
        return 0
    fi
    
    local cmd="$1"
    shift
    local path
    
    # 1) Quick shortcut: PATH of the current user (covers the common 0755 case)
    path=$(safe_capture command -v "$cmd")
    if [[ -n "$path" ]]; then
        echo "$path"
        return 0
    fi
    
    # 2) Known FS paths: EXISTENCE check, not -x
    #    (allows detecting 0700 root-only binaries that only sudo can run)
    local dirs="/sbin /usr/sbin /bin /usr/bin /usr/local/sbin /usr/local/bin"
    # Specific extra paths, if passed as additional arguments
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
# GLOBAL VARIABLES
# Commands that the Rundeck user does not recognize from its PATH, so
# their absolute path must be identified
###############################################################################

IP_CMD=$(find_command_user ip)
IFCONFIG_CMD=$(find_command_user ifconfig)
DMIDECODE_CMD=$(find_command_privileged dmidecode)
PVEVERSION_CMD=$(find_command_user pveversion)
FDISK_CMD=$(find_command_privileged fdisk)
ROUTE_CMD=$(find_command_user route)
LSBRELEASE_CMD=$(find_command_user lsb_release)

###############################################################################
# Read a file located in /sys
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
# Normalize manufacturer
###############################################################################

normalize_manufacturer() {
    local manufacturer="$1"

    case "$manufacturer" in
        "Dell Inc."|"Dell Inc")
            echo "Dell"
            ;;
        "Hewlett-Packard"|"Hewlett Packard")
            echo "HP"
            ;;
        "HPE")
            echo "HPE"
            ;;
        "LENOVO")
            echo "Lenovo"
            ;;
        "Intel Corporation")
            echo "Intel"
            ;;
        *)
            echo "$manufacturer"
            ;;
    esac
}

###############################################################################
# Get specific information via DMI (sysfs with fallback to dmidecode)
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
        system-sku-number)
            sysfs_file="/sys/class/dmi/id/product_sku"
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

    # Read the sysfs file
    if [[ -e "$sysfs_file" ]]; then
        value=$(read_sysfs "$sysfs_file")
    fi

    # Read the dmidecode output
    if [[ -z "$value" && -n "$DMIDECODE_CMD" ]]; then
        value=$(safe_capture sudo "$DMIDECODE_CMD" -s "$field")
    fi

    # If $1 is the manufacturer, normalize it
    if [[ "$field" == "system-manufacturer" ]]; then
        value=$(normalize_manufacturer "$value")
    fi

    echo "$value"
}

###############################################################################
# Get and validate the system serial number from SMBIOS.
###############################################################################
get_system_serial() {
    local serial

    serial=$(get_dmi system-serial-number)

    case "$serial" in
        ""|"To Be Filled By O.E.M."|"Default string"|"Unknown"|"Not Specified"|"None"|"0123456789"|"S123456789"|"............")
            echo ""
            ;;
        *)
            echo "$serial"
            ;;
    esac
}

###############################################################################
# Get the Part Number (P/N) or SKU Number of the physical system.
###############################################################################
get_part_number() {
    local pn

    pn=$(get_dmi system-sku-number)

    # Fallback for old kernels or outdated dmidecode versions that do not support product_sku
    if [[ -z "$pn" && -n "$DMIDECODE_CMD" ]]; then
        pn=$(safe_capture sudo "$DMIDECODE_CMD" -t 1 -t 2 -t 3 | awk '
            /SKU Number|Part Number/ {
                val = $0
                sub(/^[^:]+: */, "", val)
                if (val != "" && val !~ /Not Specified|To [Bb]e [Ff]illed|O\.E\.M\.|Default string|^None$/) {
                    print val
                    exit
                }
            }
        ')
    fi

    # Final cleanup of spaces or line breaks
    if [[ -n "$pn" ]]; then
        echo "$pn" | sed 's/^[ \t]*//;s/[ \t]*$//'
    else
        echo ""
    fi
}

###############################################################################
# Get and validate the system UUID, with fallbacks for old hardware.
###############################################################################
get_system_uuid() {
    local uuid
    uuid=$(get_dmi system-uuid)

    # Convert to lowercase in a bash 3.0 compatible way
    if [[ -n "$uuid" ]]; then
        uuid=$(echo "$uuid" | tr '[:upper:]' '[:lower:]')
    fi

    # Filter invalid values returned by faulty DMI implementations
    case "$uuid" in
        ""|"not settable"|"not present"|"none"|"unknown"|"to be filled by o.e.m."|"03000200-0400-0500-0006-000700080009"|"00000000-0000-0000-0000-000000000000"|"ffffffff-ffff-ffff-ffff-ffffffffffff")
            uuid=""
            ;;
    esac

    # If it is valid, return it
    if [[ -n "$uuid" ]]; then
        echo "$uuid"
        return
    fi

    # Fallback 1: dbus machine-id (present in CentOS 4+)
    if [[ -r /var/lib/dbus/machine-id ]]; then
        uuid=$(safe_capture cat /var/lib/dbus/machine-id)
        if [[ -n "$uuid" && ${#uuid} -eq 32 ]]; then
            echo "${uuid:0:8}-${uuid:8:4}-${uuid:12:4}-${uuid:16:4}-${uuid:20:12}"
            return
        fi
    fi

    # Fallback 2: systemd machine-id (modern distros)
    if [[ -r /etc/machine-id ]]; then
        uuid=$(safe_capture cat /etc/machine-id)
        if [[ -n "$uuid" && ${#uuid} -eq 32 ]]; then
            echo "${uuid:0:8}-${uuid:8:4}-${uuid:12:4}-${uuid:16:4}-${uuid:20:12}"
            return
        fi
    fi

    # Fallback 3: Generation of a pseudo-stable UUID based on MAC and Hostname
    # md5sum is in coreutils, awk is mandatory.
    local mac
    mac=$(get_mac_address)
    local hostname
    hostname=$(safe_capture hostname)
    
    if [[ -n "$mac" || -n "$hostname" ]]; then
        uuid=$(echo "${mac}${hostname}" | md5sum | awk '{print $1}')
        if [[ -n "$uuid" && ${#uuid} -ge 32 ]]; then
            echo "${uuid:0:8}-${uuid:8:4}-${uuid:12:4}-${uuid:16:4}-${uuid:20:12}"
            return
        fi
    fi
}

###############################################################################
# Machine type detection
###############################################################################

is_vm_signature() {
    # Receives a lowercase string and determines whether it contains any
    # known hypervisor/VM signature (manufacturer, product name, bios vendor, etc).
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

is_kvm_hypervisor() {
    # 1. Socket validation (Bash builtin): ultra fast, without forks or subprocesses.
    if [[ -S /var/run/libvirt/libvirt-sock || -S /run/libvirt/libvirt-sock ]]; then
        return 0
    fi

    # 2. systemd daemon validation (1 subprocess): very fast, detects an active libvirtd.
    if exists systemctl && systemctl is-active --quiet libvirtd 2>/dev/null; then
        return 0
    fi

    # 3. Traditional KVM/libvirt via command (2 subprocesses + timeout/permissions risk).
    if exists virsh && with_timeout virsh -c qemu:///system list --all >/dev/null 2>&1; then
        return 0
    fi

    # 4. Raw process search (2 subprocesses): catches KVMs without libvirt.
    # shellcheck disable=SC2009
    # grep >/dev/null (not -q) is used to consume the whole output of ps: with pipefail,
    # a grep -q that closes the pipe early can give ps a 141 (SIGPIPE) and
    # produce a false negative.
    if ps -e -o comm= 2>/dev/null | grep '^qemu-system-' >/dev/null; then
        return 0
    fi

    return 1
}

is_hypervisor() {
    # NOTE: if the system lacks the timeout command (old coreutils) and
    # virsh/xl hang because of an unresponsive daemon, the script could stay
    # hung indefinitely. Low risk in practice.
    
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

    # KVM / libvirt
    if is_kvm_hypervisor; then
        return 0
    fi

    return 1
}

detect_guest_type() {
    # Prints "VM" or "Container" if the system runs virtualized/containerized;
    # prints nothing if no virtualization is detected (physical machine).
    # It must be evaluated BEFORE is_hypervisor: a guest can expose libvirt
    # sockets, qemu processes, etc. (nested virt, tools) and is not a physical host.

    # VM/container detection through systemd (authoritative result)
    if exists systemd-detect-virt; then
        local virt
        virt=$(safe_capture systemd-detect-virt)

        if [[ -z "$virt" || "$virt" == "none" ]]; then
            return 0
        fi

        case "$virt" in
            docker|podman|container-other|lxc|lxc-libvirt|openvz|systemd-nspawn)
                echo "Container"
                ;;
            *)
                echo "VM"
                ;;
        esac
        return 0
    fi

    # Containers without systemd
    if [[ -f /.dockerenv || -f /run/.containerenv ]]; then
        echo "Container"
        return 0
    fi

    if grep -qa '\(docker\|lxc\|container\|kubepods\)' /proc/1/cgroup 2>/dev/null; then
        echo "Container"
        return 0
    fi

    # VM detection through DMI (sysfs with fallback to dmidecode via get_dmi)
    if [[ -r /sys/class/dmi/id/product_name || -n "$DMIDECODE_CMD" ]]; then
        local product_name sys_vendor bios_vendor
        product_name=$(get_dmi system-product-name | tr '[:upper:]' '[:lower:]')
        sys_vendor=$(get_dmi system-manufacturer | tr '[:upper:]' '[:lower:]')
        bios_vendor=$(get_dmi bios-vendor | tr '[:upper:]' '[:lower:]')

        if is_vm_signature "$product_name $sys_vendor $bios_vendor"; then
            echo "VM"
        fi
        return 0
    fi

    # Last resort: CPU flag
    if grep -qi hypervisor /proc/cpuinfo 2>/dev/null; then
        echo "VM"
    fi
}

get_machine_type() {
    # Priority: guest (VM/Container) > Hypervisor > Dedicated
    local guest_type
    guest_type=$(detect_guest_type)

    if [[ -n "$guest_type" ]]; then
        echo "$guest_type"
        return 0
    fi

    if is_hypervisor; then
        echo "Hypervisor"
        return 0
    fi

    echo "Dedicated"
}

###############################################################################
# Operating System
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
        "Kali GNU/Linux")
            echo "Kali Linux" ;;
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
    # Detects hypervisor software/platform.
    # Returns "Name<US>Version", empty string if not applicable.

    # Proxmox VE on top of Debian
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
    # Strip wrapping single or double quotes, if any
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"

    echo "$value"
}

parse_release_file() {
    # Parses files such as:
    #   "CentOS Linux release 7.3.1611 (Core)"  -> "CentOS Linux<US>7.3.1611"
    #   "CentOS release 6.10 (Final)"            -> "CentOS<US>6.10"
    #   "Red Hat Enterprise Linux Server release 5.11 (Tikanga)" -> "Red Hat Enterprise Linux Server<US>5.11"
    # Logic:
    #   - Removes the word "release" and everything between parentheses
    #   - The last resulting token is the version
    #   - The rest is the name
    local file="$1"
    local result
    result=$(safe_capture awk '
        NR == 1 {
            # Remove " (....)" at the end
            sub(/ *\([^)]*\)/, "")
            # Remove the word "release" (with surrounding spaces)
            sub(/ release /, " ")
            # Remove leftover spaces at the start and at the end
            gsub(/^[[:space:]]+|[[:space:]]+$/, "")
            # The last field is the version, the rest is the name
            version = $NF
            name = ""
            for (i = 1; i < NF; i++) {
                name = (i == 1) ? $i : name " " $i
            }
            printf "%s\x1f%s", name, version
        }
    ' "$file")
    printf '%s' "$result"
}

get_os_info() {
    # Detects OS and version in a single place. Returns "OS<US>VERSION".
    # If the node is a hypervisor (e.g. Proxmox), it appends the hypervisor
    # name/version separated by a comma: "Debian, Proxmox<US>11, 7.4-3"
    local os_name=""
    local os_version=""
    if [[ -f /etc/redhat-release ]]; then
        IFS=$'\x1f' read -r os_name os_version <<< "$(parse_release_file /etc/redhat-release)"
        os_name=$(normalize_os_name "$os_name")
    elif [[ -f /etc/centos-release ]]; then
        IFS=$'\x1f' read -r os_name os_version <<< "$(parse_release_file /etc/centos-release)"
        os_name=$(normalize_os_name "$os_name")
    elif [[ -f /etc/os-release ]]; then
        local name_raw version_id_raw version_raw
        name_raw=$(get_os_release_field "NAME")
        version_id_raw=$(get_os_release_field "VERSION_ID")
        version_raw=$(get_os_release_field "VERSION")
        os_name=$(normalize_os_name "$name_raw")
        if [[ "$os_name" == "Debian" && -f /etc/debian_version ]]; then
            os_version=$(safe_capture cat /etc/debian_version)
        elif [[ "$os_name" == "Ubuntu" ]]; then
            os_version="${version_raw%% *}"
        else
            os_version="${version_id_raw:-${version_raw:-N/A}}"
        fi
    elif [[ -n "$LSBRELEASE_CMD" ]]; then
        os_name=$(safe_capture "$LSBRELEASE_CMD" -si)
        os_version=$(safe_capture "$LSBRELEASE_CMD" -sr)
    elif [[ -f /etc/debian_version ]]; then
        os_name="Debian"
        os_version=$(safe_capture cat /etc/debian_version)
    elif [[ -f /etc/system-release ]]; then
        IFS=$'\x1f' read -r os_name os_version <<< "$(parse_release_file /etc/system-release)"
        os_name=$(normalize_os_name "$os_name")
    else
        os_name=$(safe_capture uname -s)
        os_version=$(safe_capture uname -r)
    fi

    local hv_info hv_name hv_version
    hv_info=$(get_hypervisor_info)
    if [[ -n "$hv_info" ]]; then
        hv_name="${hv_info%%$'\x1f'*}"
        hv_version="${hv_info#*$'\x1f'}"
        os_name="${os_name}, ${hv_name}"
        os_version="${os_version}, ${hv_version}"
    fi

    printf '%s\x1f%s' "$os_name" "$os_version"
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
# CPU topology sources (lscpu / /proc/cpuinfo)
###############################################################################

get_lscpu_field() {
    # Returns the value of the first lscpu field whose name matches
    # exactly any of the received ones ($1, separated by '|').
    # The match is exact (not by prefix) so as not to confuse
    # "Socket(s)" with "Socket(s) per book" (s390x), and it tolerates the hierarchical
    # indentation of util-linux >= 2.37 ("    Core(s) per socket:  16").
    if ! exists lscpu; then
        return 0
    fi
    lscpu 2>/dev/null | awk -v names="$1" '
        BEGIN {
            n = split(names, list, "|")
            for (i = 1; i <= n; i++) wanted[list[i]] = 1
        }
        {
            sep = index($0, ":")
            if (sep == 0) next
            key = substr($0, 1, sep - 1)
            sub(/^[ \t]+/, "", key); sub(/[ \t]+$/, "", key)
            if (!(key in wanted)) next
            value = substr($0, sep + 1)
            sub(/^[ \t]+/, "", value); sub(/[ \t]+$/, "", value)
            print value
            exit
        }
    ' || true
}

get_cpuinfo_field() {
    # Returns the value of the first occurrence of the field $1 in /proc/cpuinfo.
    # On x86 the per-physical-package fields ("cpu cores", "siblings") are
    # repeated identically in every logical processor, the first one is enough.
    if [[ ! -r /proc/cpuinfo ]]; then
        return 0
    fi
    awk -v name="$1" '
        {
            sep = index($0, ":")
            if (sep == 0) next
            key = substr($0, 1, sep - 1)
            sub(/[ \t]+$/, "", key)
            if (key != name) next
            value = substr($0, sep + 1)
            sub(/^[ \t]+/, "", value); sub(/[ \t]+$/, "", value)
            print value
            exit
        }
    ' /proc/cpuinfo 2>/dev/null || true
}

get_cpu_logical_total() {
    # Total logical CPUs visible to the operating system.
    local total
    total=$(get_lscpu_field "CPU(s)")
    if ! is_positive_int "$total" && [[ -r /proc/cpuinfo ]]; then
        total=$(safe_capture grep -c '^processor' /proc/cpuinfo)
    fi
    echo "$total"
}

per_socket_from_total() {
    # Splits a system total ($1) among the sockets ($2). It is the last
    # resort when there is no exposed topology (VMs with trimmed CPUID):
    # if the split is not exact it is truncated, and if it gives 0 no value
    # is emitted so as not to report a made-up figure.
    local total="$1" sockets="$2"
    if is_positive_int "$total" && is_positive_int "$sockets" && (( total >= sockets )); then
        echo $(( total / sockets ))
    fi
}

###############################################################################
# CPU model
###############################################################################

get_cpu_model() {
    local model
    model=$(get_lscpu_field "Model name")
    if [[ -z "$model" ]]; then
        model=$(get_cpuinfo_field "model name")
    fi
    echo "$model"
}

###############################################################################
# CPU sockets
###############################################################################

get_cpu_sockets() {
    # It is computed before cores/threads: both use it as a last-resort
    # divisor. On ARM lscpu can report "Socket(s): -".
    local sockets
    sockets=$(get_lscpu_field "Socket(s)|CPU socket(s)")
    if ! is_positive_int "$sockets" && [[ -r /proc/cpuinfo ]]; then
        sockets=$(awk -F': ' '/^physical id/ {print $2}' /proc/cpuinfo 2>/dev/null | sort -u | wc -l) || true
    fi
    if ! is_positive_int "$sockets"; then
        sockets=1
    fi
    echo "$sockets"
}

###############################################################################
# CPU cores (per socket)
###############################################################################

get_cpu_cores() {
    # Physical cores PER SOCKET (contract of the "Cores (per socket)" column).
    # $1: detected sockets, used only as a last-resort divisor.
    local sockets="$1" cores
    cores=$(get_lscpu_field "Core(s) per socket|Core(s) per cluster")
    if ! is_positive_int "$cores"; then
        # x86: "cpu cores" is already per physical package
        cores=$(get_cpuinfo_field "cpu cores")
    fi
    if ! is_positive_int "$cores"; then
        # Without exposed topology 1 thread per core is assumed (typical VM case)
        cores=$(per_socket_from_total "$(get_cpu_logical_total)" "$sockets")
    fi
    echo "$cores"
}

###############################################################################
# CPU threads (per socket)
###############################################################################

get_cpu_threads() {
    # Logical threads PER SOCKET (contract of the "Threads (per socket)" column).
    # $1: detected sockets, $2: cores per socket already computed.
    local sockets="$1" cores="$2" threads="" threads_per_core
    threads_per_core=$(get_lscpu_field "Thread(s) per core")
    if is_positive_int "$threads_per_core" && is_positive_int "$cores"; then
        threads=$(( threads_per_core * cores ))
    fi
    if ! is_positive_int "$threads"; then
        # x86: "siblings" are the logical CPUs per physical package
        threads=$(get_cpuinfo_field "siblings")
    fi
    if ! is_positive_int "$threads"; then
        threads=$(per_socket_from_total "$(get_cpu_logical_total)" "$sockets")
    fi
    echo "$threads"
}

###############################################################################
# Memory
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
# Determines whether a /sys/block device corresponds to a valid physical/virtual
# disk (excludes CD-ROM, software RAID, LVM, loop, RAM, etc.)
###############################################################################

is_real_disk() {
    local d="$1"
    local dev
    dev=$(basename "$d")

    # Discard virtual devices known by name
    case "$dev" in
        loop*|ram*|fd*|zram*|sr*|md*|dm-*|nbd*|rbd*|nvme*c*n*)
            return 1
            ;;
    esac

    # Ignore partitions (in case /sys/block/nvme0n1p1 or sda1 is passed)
    if [[ -f "$d/partition" ]]; then
        return 1
    fi

    # If the device has sub-devices (slaves), it is virtual (RAID, LVM, Multipath, etc.)
    if [[ -d "$d/slaves" && "$(safe_capture ls -A "$d/slaves")" ]]; then
        return 1
    fi

    # Ignore devices of size 0 (empty readers, interfaces without a disk)
    if [[ -f "$d/size" ]]; then
        local size
        size=$(safe_capture cat "$d/size")
        if [[ "${size:-0}" -eq 0 ]]; then
            return 1
        fi
    fi

    # Ignore CD/DVD or other non-disk SCSI types (Type 5 = CD-ROM)
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
# Gets the size (in bytes) of all the physical/valid disks
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

        sudo "$FDISK_CMD" -l 2>/dev/null |
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
# Total disk capacity (GB)
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
# Disk summary
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
# Groups repeated RAID levels into a readable summary
# E.g.: input "RAID1\nRAID1\nRAID5" -> output "RAID1 x2 + RAID5"
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
# Hardware RAID: Dell / LSI (perccli, megacli)
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
            # Expected format (table): "0/0   RAID1  Optl  RW ..."
            output=$(safe_capture with_timeout "${cmd[@]}" /call/vall show)
            printf '%s\n' "$output" |
                awk '/^[0-9]+\/[0-9]+[[:space:]]+RAID/ { print $2 }' || true
            ;;
        *[Mm]ega[Cc]li*)
            # Expected format:
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
# Hardware RAID: HP / HPE (ssacli, hpssacli, hpacucli)
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
    # Expected format: "      Fault Tolerance: RAID 1"
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
# Hardware RAID: Adaptec (arcconf) - common in some Lenovo/Supermicro
###############################################################################
get_raid_adaptec() {
    local bin
    bin=$(find_command_privileged arcconf)
    if [[ -z "$bin" ]]; then
        return 0
    fi
    local -a cmd=(sudo "$bin")
    local output
    # Expected format: "RAID level                              : 5"
    # Controller 1 is assumed; on machines with multiple controllers
    # it would be better to iterate, but it is not the common case.
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
# Software RAID: mdadm (via /proc/mdstat)
# Important for old systems without a dedicated controller and for
# hypervisors/dedicated servers that use software RAID.
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
# Looks for known RAID controller signatures in the vendor/model of the disks
# visible in /sys/block (they are filled in by the controller firmware, not the
# real physical disk, so they reveal the presence of hardware RAID
# even when the OS sees a single logical disk).
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
        # Trim leftover spaces (SCSI INQUIRY data comes
        # filled with padding spaces, e.g.: "DELL    ")
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
# Fallback: no RAID tool detected.
# - If a RAID controller signature is detected (via vendor/model), there is RAID
#   but the level could not be determined -> "Unknown (controller X)".
# - If there is no signature and only one physical disk, it is reasonable to assume no RAID.
# - If there is no signature but there are multiple disks, there is no reliable way to
#   know -> it is left empty (the caller will show it as N/A).
###############################################################################
get_raid_fallback() {
    local sig n

    sig=$(get_raid_controller_signature)
    if [[ -n "$sig" ]]; then
        echo "Unknown (controller $sig)"
        return
    fi

    n=$(get_disk_sizes_bytes 2>/dev/null | wc -l) || true
    if [[ "$n" -le 1 ]]; then
        echo "No RAID"
    fi
}

###############################################################################
# Orchestrator: tries each provider in order until a result is obtained
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
# IP <-> integer conversion (for network/mask calculations)
# The 10# prefix forces decimal base on each octet, preventing bash from
# interpreting octets with leading zeros (e.g. 010) as octal.
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
# Counts the 1 bits of a dotted-decimal mask (e.g.: 255.255.255.0 -> 24)
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
# Determines whether a network interface is "real" for inventory purposes.
# Synthetic/ephemeral interfaces that add no value to a datacenter
# inventory are excluded: loopback, veth (containers), docker0/br-*
# (Docker bridges), virbr* (libvirt internal NAT bridge, not a
# real uplink), tun/tap, vnet* (per-VM vnetXX interfaces in KVM/libvirt),
# fwbr*/fwln*/fwpr* (per-VM firewall bridges generated by Proxmox).
# bond* and vmbr* are "real" interfaces from the inventory point of view
# (bond = logical NIC through LACP/teaming; vmbr = real uplink bridge in Proxmox,
# analogous to a physical network bridge). Note that the physical "slave"
# NICs of a bond (e.g. eth0/eth1 within bond0) will also show up
# in the listing, usually without their own IP (the IP lives on the bond) - this
# is intentional: it documents the real physical cabling of the node.
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
# Reads the operstate (up/down/unknown) of an interface from sysfs.
# If it is not available, it returns "unknown".
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
# Enumerates the names of the "real" network interfaces (see is_real_nic),
# one per line. It tries /sys/class/net -> ip -> ifconfig, in that order.
# Base function shared by get_primary_interface and get_network_interfaces.
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
# Primary interface: the one that has the default route.
# It tries ip route -> route -n -> /proc/net/route (universal, without
# external dependencies) -> first real interface that is UP.
# NOTE: NOT USED DIRECTLY IN THIS SCRIPT. KEPT FOR LATER USE.
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
# Gets the IP and prefix of an interface in "ip/prefix" format (e.g.: 10.0.0.5/24).
# Internal base function: it tries 'ip', then 'ifconfig' (old and new format).
# It is the only function that knows how to parse the ip/ifconfig output; get_ip_address
# and get_network_cidr only derive from this result.
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
# Validates that a prefix is an integer between 0 and 32
###############################################################################
is_valid_prefix() {
    local prefix="$1"
    case "$prefix" in
        ''|*[!0-9]*) return 1 ;;
    esac
    (( prefix >= 0 && prefix <= 32 ))
}

###############################################################################
# IP (IPv4) of an interface (by default, the primary one)
###############################################################################
get_ip_address() {
    local iface="${1:-$(get_primary_interface)}"
    local raw
    raw=$(get_ip_cidr_raw "$iface")
    if [[ -z "$raw" ]]; then return 0; fi

    echo "${raw%/*}"
}

###############################################################################
# IP network in CIDR format (network address, not the host IP): x.x.x.x/YY
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
    local ip_int mask_int network_int
    ip_int=$(ip_to_int "$ip")
    mask_int=$(( (0xFFFFFFFF * (2 ** (32 - prefix))) & 0xFFFFFFFF ))
    network_int=$(( ip_int & mask_int ))
    echo "$(int_to_ip "$network_int")/$prefix"
}

###############################################################################
# MAC address of an interface (by default, the primary one)
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
# Walks through all the "real" interfaces (see is_real_nic) and builds 5 lists
# aligned by index, separated by ", ": INTERFACES, INTERFACES_STATUS,
# IP, IP_NETWORK, MAC.
#
# E.g. if the node has eth0 (up) and eth1 (down):
#   INTERFACES        = "eth0, eth1"
#   INTERFACES_STATUS = "up, down"
#   IP                = "10.0.0.5, N/A"
#   IP_NETWORK        = "10.0.0.0/24, N/A"
#   MAC               = "ab:cd:ef:12:34:56, 98:76:54:32:ab:cd"
#
# An interface without a configured IP (e.g. a slave NIC of a bond, or a
# down interface) still shows up in INTERFACES/INTERFACES_STATUS/MAC, but
# with "N/A" in its IP/IP_NETWORK position - this is intentional to
# keep the 5 indexes aligned with each other.
#
# Returns the 5 lists separated by \x1f (do not use ", " as the separator
# here, since the individual lists ALREADY contain ", ").
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
# FQDN of the machine
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
# Machine name
###############################################################################

MACHINE_NAME=$(safe_capture hostname)


###############################################################################
# Machine type detection
###############################################################################

MACHINE_TYPE=$(get_machine_type)


###############################################################################
# UUID
###############################################################################

UUID=$(get_system_uuid)


###############################################################################
# Operating system
###############################################################################

OS_INFO=$(get_os_info)

HOST_OS=$(get_host_os_name "$OS_INFO")
HOST_OS_VERSION=$(get_host_os_version "$OS_INFO")
HYPERVISOR_OS=$(get_hypervisor_os_name "$OS_INFO")
HYPERVISOR_OS_VERSION=$(get_hypervisor_os_version "$OS_INFO")

###############################################################################
# Kernel
###############################################################################

KERNEL=$(uname -r)


###############################################################################
# Specific hardware information
###############################################################################

MANUFACTURER=""
MODEL=""
SERIAL=""
SERVICE_TAG=""
PART_NUMBER=""

if [[ "$MACHINE_TYPE" == "Hypervisor" || "$MACHINE_TYPE" == "Dedicated" ]]; then
    MANUFACTURER=$(get_dmi system-manufacturer)
    MODEL=$(get_dmi system-product-name)
    SERIAL=$(get_system_serial)
    PART_NUMBER=$(get_part_number)

    if [[ "$MANUFACTURER" == *Dell* ]]; then
        SERVICE_TAG="$SERIAL"
        SERIAL=""
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

CPU_MODEL=$(get_cpu_model)
CPU_SOCKETS=$(get_cpu_sockets)
CPU_CORES=$(get_cpu_cores "$CPU_SOCKETS")
CPU_THREADS=$(get_cpu_threads "$CPU_SOCKETS" "$CPU_CORES")

###############################################################################
# Memory
###############################################################################

RAM_GB=$(get_ram_gb)

###############################################################################
# Storage
###############################################################################

STORAGE_CAPACITY=$(get_disk_capacity_gb)

STORAGE_DISKS=""

if [[ "$MACHINE_TYPE" == "VM" ]]; then
    STORAGE_DISKS=$(get_disk_layout)
fi

###############################################################################
# RAID
###############################################################################

RAID=""
if [[ "$MACHINE_TYPE" == "Hypervisor" || "$MACHINE_TYPE" == "Dedicated" ]]; then
    RAID=$(get_raid_info)
fi


###############################################################################
# Network
###############################################################################

NET_TABLE=$(get_network_table)
IFS=$'\x1f' read -r INTERFACES INTERFACES_STATUS IP IP_NETWORK MAC <<< "$NET_TABLE"

FQDN=$(get_fqdn)




###############################################################################
# Output data (must keep a 1-to-1 position match between OUTPUT_KEYS and OUTPUT_VALUES)
###############################################################################

OUTPUT_KEYS=(
    "Machine name"
    "Machine type"
    "Host OS"
    "Host OS version"
    "Hypervisor OS"
    "Hypervisor OS version"
    "Kernel"
    "FQDN"
    "Interfaces"
    "Interfaces status"
    "IP"
    "IP network"
    "MAC"
    "CPU model"
    "Cores (per socket)"
    "Threads (per socket)"
    "Sockets"
    "RAM (GB)"
    "Disks"
    "Visible capacity (GB)"
    "RAID"
    "Manufacturer"
    "Model"
    "Serial Number"
    "Service Tag"
    "P/N"
    "UUID"
    "BIOS version"
    "BIOS date"
)

OUTPUT_VALUES=(
    "$MACHINE_NAME"
    "$MACHINE_TYPE"
    "$HOST_OS"
    "$HOST_OS_VERSION"
    "$HYPERVISOR_OS"
    "$HYPERVISOR_OS_VERSION"
    "$KERNEL"
    "$FQDN"
    "$INTERFACES"
    "$INTERFACES_STATUS"
    "$IP"
    "$IP_NETWORK"
    "$MAC"
    "$CPU_MODEL"
    "$CPU_CORES"
    "$CPU_THREADS"
    "$CPU_SOCKETS"
    "$RAM_GB"
    "$STORAGE_DISKS"
    "$STORAGE_CAPACITY"
    "$RAID"
    "$MANUFACTURER"
    "$MODEL"
    "$SERIAL"
    "$SERVICE_TAG"
    "$PART_NUMBER"
    "$UUID"
    "$BIOS_VERSION"
    "$BIOS_DATE"
)


###############################################################################
# Prints a value for the key-value output.
# The value is wrapped in double quotes.
# Inner double quotes are escaped by doubling them.
###############################################################################

key_value_field() {
    local value="$1"

    value=${value//\"/\"\"}

    printf '"%s"' "$value"
}


###############################################################################
# Key-value output
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
# Output
###############################################################################

print_key_value_output

