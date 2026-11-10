#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
export CB_LOG_LEVEL="$(bashio::config 'log_level' 'info')"
export CB_DATA=/data
bashio::log.info "Cluster Builder starting (UI through Ingress, machines on port 8808)"
exec python3 -m builder
