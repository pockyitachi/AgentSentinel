# Minimal derived-image repair for ThanksgivingPrepTask initialization.
#
# Patch the installed source in place so the derived image changes exactly one
# import line even when the parent image predates other repository-only edits.
# Pass the immutable parent digest through BASE_IMAGE for formal runs.
ARG BASE_IMAGE=mobile_world:reset
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG PARENT_IMAGE_DIGEST=sha256:2038dc91b8a4288d7f66d8d843f970487903483cd81d76fd64be9c3ba658925c
ARG PATCH_SOURCE_REVISION=7fb9cc4c5265283b2dd2403d8160c0facae725f6

LABEL org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.base.digest="${PARENT_IMAGE_DIGEST}" \
      org.opencontainers.image.source="https://github.com/pockyitachi/AgentSentinel" \
      org.opencontainers.image.revision="${PATCH_SOURCE_REVISION}" \
      io.agentsentinel.patch.id="thanksgiving-reset-chrome-import" \
      io.agentsentinel.patch.target="/app/service/src/mobile_world/tasks/definitions/gmail/thanksgiving_prep.py" \
      io.agentsentinel.patch.parent-source-sha256="ba4720794818f504fccaa5742d5d75a52e2733274053f0568e015b09fc64d694" \
      io.agentsentinel.patch.source-sha256="7f6e049d9ee345fd031555bc25fad82707c3c42dc21583da9a4a28c510e74eb4" \
      io.agentsentinel.patch.operation="add-reset-chrome-to-time-sync-import"

RUN set -eu; \
    target=/app/service/src/mobile_world/tasks/definitions/gmail/thanksgiving_prep.py; \
    echo "parent-source $(sha256sum "$target" | cut -d' ' -f1)"; \
    grep -n -E 'time_sync|reset_chrome' "$target"; \
    echo 'ba4720794818f504fccaa5742d5d75a52e2733274053f0568e015b09fc64d694  /app/service/src/mobile_world/tasks/definitions/gmail/thanksgiving_prep.py' \
      | sha256sum --check --strict; \
    test "$(grep -Fxc 'from mobile_world.runtime.app_helpers.system import enable_auto_time_sync' "$target")" -eq 1; \
    test "$(grep -Fc 'reset_chrome(controller)' "$target")" -eq 1; \
    sed -i \
      's/^from mobile_world\.runtime\.app_helpers\.system import enable_auto_time_sync$/from mobile_world.runtime.app_helpers.system import enable_auto_time_sync, reset_chrome/' \
      "$target"; \
    test "$(grep -Fxc 'from mobile_world.runtime.app_helpers.system import enable_auto_time_sync, reset_chrome' "$target")" -eq 1; \
    echo '7f6e049d9ee345fd031555bc25fad82707c3c42dc21583da9a4a28c510e74eb4  /app/service/src/mobile_world/tasks/definitions/gmail/thanksgiving_prep.py' \
      | sha256sum --check --strict; \
    echo "patched-source $(sha256sum "$target" | cut -d' ' -f1)"
