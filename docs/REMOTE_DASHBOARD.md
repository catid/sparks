# Run the dashboard on a third Linux host

The dashboard can run on an ordinary, always-on Linux host instead of using
RAM on either Spark. In this placement the collector:

- samples Spark 1 and Spark 2 through separate, read-only SSH probes;
- scrapes the single TP2 vLLM metrics endpoint on Spark 1;
- retains its short in-memory history and serves the UI on the third host; and
- has no role in starting, stopping, or recovering the model service.

The existing Spark 1 placement remains the default. Setting
`SPARK1_SSH_HOST` opts into remote Spark 1 collection; leaving it unset keeps
the direct local probe.

## Security model

Use a dedicated key pair for the dashboard. Do not reuse an administrator key
or agent forwarding. The private key, `known_hosts`, live environment,
generated Basic-auth password, and TLS private key must remain outside the
public checkout.

The recommended SSH authorization on **each Spark** is a key forced to the
fixed probe:

```text
restrict,command="/usr/local/libexec/dgx-spark-dashboard-probe" ssh-ed25519 AAAA... dashboard-collector
```

`restrict` disables forwarding, PTY allocation, X11, and agent forwarding.
The forced script ignores the requested SSH command and stdin, and only emits
GPU, thermal, memory, vLLM-process, and network counters. Associate this key
with an unprivileged account that has no sudo or Docker membership.

Install the fixed command on each Spark:

```bash
cd ~/sparks
scripts/install-dashboard-probe.sh verify
scripts/install-dashboard-probe.sh install
```

Add the collector's **public** key to that account's `authorized_keys` with
the restriction above. Never copy the private key to a Spark.

Pin host keys rather than accepting a first-use prompt in an unattended
service. Collect candidate keys with `ssh-keyscan`, verify their fingerprints
through an independent trusted path, then install only the verified entries
as the collector account's mode-`0600` `~/.ssh/known_hosts`. The collector
enforces `BatchMode`, `IdentitiesOnly`, strict host-key checking, and the
configured `DASHBOARD_SSH_KNOWN_HOSTS` file.

The vLLM metrics scrape uses Spark 1's normal HTTP endpoint. Keep it on the
trusted management LAN and restrict TCP 8889 at the site firewall to approved
API clients and the collector host. Use a private overlay network or a
separately managed SSH tunnel if the management network itself is untrusted.
Do not expose vLLM or the raw dashboard collector port to the Internet.

## Prepare the third host

Install Python 3, OpenSSH client, and optionally Nginx and OpenSSL:

```bash
sudo apt-get install python3 openssh-client nginx openssl
```

Create a dedicated service account with a real home directory, then clone the
public repository as that account. The examples below call it
`dgx-dashboard`; account creation policy is distribution-specific.

Generate a dedicated Ed25519 key as the service account without storing it in
the checkout. A boot service cannot prompt for a passphrase, so this key is
unencrypted and must be protected by mode `0600`, the forced remote command,
and the dedicated local account:

```bash
sudo install -d -o dgx-dashboard -g dgx-dashboard -m 0700 \
  /home/dgx-dashboard/.ssh
sudo -u dgx-dashboard ssh-keygen -t ed25519 \
  -f /home/dgx-dashboard/.ssh/id_ed25519_dgx_dashboard \
  -N '' -C dashboard-collector
```

Copy the sanitized deployment template to a root-controlled location and
edit only the live copy:

```bash
sudo install -d -o root -g root -m 0755 /etc/dgx-spark-dashboard
sudo install -o root -g root -m 0600 \
  dashboard/dashboard.remote.env.example \
  /etc/dgx-spark-dashboard/dashboard.env
sudoedit /etc/dgx-spark-dashboard/dashboard.env
```

At minimum, set the two user-qualified SSH hosts and verify the key and
`known_hosts` paths. The TP2 endpoint should remain:

```ini
SPARK1_SSH_HOST=monitor@spark1.lan
SPARK2_SSH_HOST=monitor@spark2.lan
SPARK1_VLLM_URL=http://spark1.lan:8889
SPARK1_VLLM_ROLE=aggregate
SPARK2_VLLM_ROLE=worker
```

Test both forced probes as the service account before installing systemd:

```bash
sudo -u dgx-dashboard ssh \
  -i /home/dgx-dashboard/.ssh/id_ed25519_dgx_dashboard \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/dgx-dashboard/.ssh/known_hosts \
  monitor@spark1.lan ignored

sudo -u dgx-dashboard ssh \
  -i /home/dgx-dashboard/.ssh/id_ed25519_dgx_dashboard \
  -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/dgx-dashboard/.ssh/known_hosts \
  monitor@spark2.lan ignored
```

Each command should print `HOSTNAME=`, `GPU=`, `MEM_`, `THERMAL=`,
`VLLM_RSS=`, and `NET=` records, then exit.

## Verify and install systemd

The same hardened unit template supports both placements. The explicit mode
flag removes the Spark 1 hostname guard and validates all remote-collector
settings:

The live environment is intentionally root-readable only, so run the
installer as root while naming the unprivileged runtime account explicitly:

```bash
sudo env \
  SPARK_SERVICE_USER=dgx-dashboard \
  DASHBOARD_ENV_FILE=/etc/dgx-spark-dashboard/dashboard.env \
  scripts/install-dashboard.sh verify --remote-collector
sudo env \
  SPARK_SERVICE_USER=dgx-dashboard \
  DASHBOARD_ENV_FILE=/etc/dgx-spark-dashboard/dashboard.env \
  scripts/install-dashboard.sh enable --remote-collector
```

`enable` installs the unit but does not interrupt a running dashboard. Use
`start` when ready to launch it:

```bash
sudo env \
  SPARK_SERVICE_USER=dgx-dashboard \
  DASHBOARD_ENV_FILE=/etc/dgx-spark-dashboard/dashboard.env \
  scripts/install-dashboard.sh start --remote-collector
```

For HTTPS on a site-specific name, install Nginx in the same operation:

```bash
sudo env \
  SPARK_SERVICE_USER=dgx-dashboard \
  DASHBOARD_ENV_FILE=/etc/dgx-spark-dashboard/dashboard.env \
  DASHBOARD_WEB_HOST=spark-dashboard.lan \
  DASHBOARD_LAN_IP=192.0.2.40 \
  scripts/install-dashboard.sh start --remote-collector --web
```

Replace the documentation-only IP with the host's actual address. On a fresh
web install, the installer generates a random Basic-auth credential and keeps
it only in the root-readable `/etc/default/dgx-spark-laguna-dashboard`.
Existing live environments and certificates are preserved unless replacement
is explicit.

Check the service and authenticated API:

```bash
systemctl status dgx-spark-laguna-dashboard.service
journalctl -u dgx-spark-laguna-dashboard.service -n 100 --no-pager

dashboard_auth="$(
  sudo sed -n 's/^DASHBOARD_AUTH=//p' \
    /etc/default/dgx-spark-laguna-dashboard
)"
curl -fsS --user "${dashboard_auth}" \
  --cacert /etc/nginx/ssl/spark-dashboard.lan.crt \
  https://spark-dashboard.lan/api/status
unset dashboard_auth
```

Inspect both node hostnames, SSH errors, the aggregate endpoint state, and
fresh timestamps before cutover.

## Cut over without losing the current mode

Do not disable Spark 1's collector until the third-host service is verified.
Then move the dashboard DNS name/reverse proxy to the third host and stop only
the old dashboard unit:

```bash
# Run on Spark 1 after the remote dashboard is proven.
sudo systemctl disable --now dgx-spark-laguna-dashboard.service
```

This does not affect vLLM, the two-rank supervisor, or model recovery. To roll
back, re-enable the same unit on Spark 1; because `SPARK1_SSH_HOST` is absent
from the normal on-Spark environment, it resumes local Spark 1 collection.
