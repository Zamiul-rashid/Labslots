# A simulated lab PC: Tailscale (userspace) with Tailscale SSH + the real setup-lab-pc.sh (labsched, slot-switch).
FROM tailscale/tailscale:v1.102.3 AS ts

FROM debian:13-slim
RUN apt-get update && apt-get install -y --no-install-recommends sudo procps passwd ca-certificates \
 && rm -rf /var/lib/apt/lists/* && mkdir -p /var/lib/tailscale /var/run/tailscale
COPY --from=ts /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/
COPY setup-lab-pc.sh /setup-lab-pc.sh
COPY entry.sh /entry.sh
RUN chmod +x /entry.sh && useradd -m labadmin
CMD ["/entry.sh"]
