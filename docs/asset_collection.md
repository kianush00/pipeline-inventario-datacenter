# Rundeck Asset Collection

This document provides detailed information regarding the `asset_information.sh` script, which is executed as part of a Rundeck job across all configured nodes.

The script is responsible for extracting comprehensive details from Linux nodes, covering identity, hardware specifications, network configurations, and operational status. It is designed to be highly compatible across heterogeneous Linux systems, maintaining backward compatibility with older distributions (e.g., CentOS 4), thus ensuring reliable execution across a wide variety of environments.

---

## 1. Collected Data

The information gathered by the script is categorized into the following areas:

### 1.1. Asset Identification

- Machine Name (Hostname)
- Machine Type (Dedicated, Hypervisor, or Virtual Machine)
- UUID
- Host Operating System (OS)
- Host OS Version
- Kernel Version

**For Hypervisor nodes only:**

- Hypervisor OS
- Hypervisor OS Version

**For Physical/Hypervisor nodes:**

- Manufacturer
- Model
- Part Number (P/N) (Non-Dell systems)
- Serial Number (Non-Dell systems)
- Service Tag (Dell systems)

### 1.2. Hardware

- BIOS Version
- BIOS Release Date
- CPU Model
- CPU Cores
- CPU Threads
- CPU Sockets
- RAM (GB)
- Visible Storage Capacity (GB)

**For Physical/Hypervisor nodes:**

- RAID Configuration

**For Virtual Machine nodes:**

- Virtual Disks

### 1.3. Network

- Network Interfaces
- Interface Status (up/down)
- MAC Address
- IP Address
- IP Network (x.x.x.x/YY)
- FQDN (Fully Qualified Domain Name)

### 1.4. Operation *(Pending Implementation)*

- Monitored Status (yes/no)
- Monitoring Tool (e.g., Cacti, Nagios, Prometheus)
- Backup Status (yes/no)
- Backup Method (e.g., systemd timers, PBS, Veeam, custom scripts)
- Backup Dates
- Active Firewall Status (yes/no)
- Firewall Backend (e.g., ufw, iptables)
- Firewall Rules

---

## 2. Minimum Node Requirements

To ensure successful execution, the target nodes must meet the following baseline requirements:

- **Operating System:** Linux (CentOS/RHEL 4+, Debian 3+, Ubuntu 4.10+, or equivalent legacy distributions).
- **Bash Version:** 3.0 or higher (requires support for `[[`, parameter expansion, and process substitution `<(...)`).
- **Kernel Version:** 2.6.9 or higher (relies on the presence of `/proc/cpuinfo`, `/proc/meminfo`, `/proc/net/route`, `/proc/mdstat`, `/sys/block`, `/sys/class/net`, and `/sys/class/dmi`).
- **Base Tools (Mandatory):**
  - `awk` (with basic BRE support, such as `mawk`), `sed`, `grep`, `sort`, `cat`, `tr`, `wc`, `uname`, and `hostname`.
  - The **Rundeck user** must be configured with passwordless `sudo` (`NOPASSWD`) privileges for the following binaries: `dmidecode`, `fdisk`, RAID controllers (e.g., `perccli`, `megacli`, `ssacli`, `arcconf`), and optionally `cat` (for reading restricted files within `/sys`).
- **Network Tools (At least one set required):**
  - `ip` (`iproute2` package)
  - `ifconfig` + `route` (`net-tools` package)
  - *Fallback:* `/proc/net/route` + `/sys/class/net` if standard binaries are missing.

---

## 3. Important Considerations

1. **Privilege Escalation:** The script invokes `sudo` in specific scenarios (e.g., reading hardware DMI tables). It strictly assumes that the target nodes are configured to allow the Rundeck user to execute `sudo` non-interactively without prompting for a password.
2. **Strict Bash Mode:** The script is hardened using `set -Eeuo pipefail`. This strict error handling must be carefully considered when contributing or modifying the codebase to prevent unintended job failures.
3. **Data Contract Maintenance:** The core maintenance of the script revolves around the `OUTPUT_KEYS` and `OUTPUT_VALUES` bash arrays. There is a strict 1-to-1 positional mapping between these two arrays. Furthermore, the strings defined in `OUTPUT_KEYS` **must** perfectly match the column names defined in `rundeck_header_list.txt`. Any deviation will cause downstream parsing and ETL errors.

---

## 4. Exporting Data to CSV

To extract the collected data for the next stage of the ETL pipeline, follow these steps in Rundeck:

1. In the job execution options, set the **"Output format"** parameter to `raw-csv`.
2. Execute the Rundeck job.
3. Navigate to the **"Activity"** tab and select the executed job.
4. Click on the **"Log Output"** button (blue icon on the left).
5. Click on the **"Execution Log"** button (icon on the right) and select **"View text"**.
6. Download the page (save the file with a `.log` extension).
7. Pass the downloaded `.log` file as an argument to the parser script:

   ```bash
   python3 parse_job_output.py raw_output.log
   ```

   > **Note:** Always verify that the `rundeck_header_list.txt` file is up-to-date and its defined column names exactly match the target headers of the master inventory before parsing.
