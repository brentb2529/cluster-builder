ARG BUILD_FROM
FROM $BUILD_FROM

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg xorriso xz-utils ipxe openssl openssh-client \
         python3 python3-aiohttp \
    && curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg \
         -o /usr/share/keyrings/proxmox-release-trixie.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/proxmox-release-trixie.gpg] http://download.proxmox.com/debian/pve trixie pve-no-subscription" \
         > /etc/apt/sources.list.d/pve.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends proxmox-auto-install-assistant \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY app /app
COPY run.sh /run.sh
RUN chmod +x /run.sh
WORKDIR /app
CMD ["/run.sh"]
