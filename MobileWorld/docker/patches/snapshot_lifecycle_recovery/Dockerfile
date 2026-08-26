# Byte-pinned, offline-derived repair for MobileWorld snapshot/emulator lifecycle recovery.
#
# Build with this directory as context and networking disabled:
#   docker build --network=none \
#     --build-arg BASE_IMAGE=sha256:4e594219474246e1c613a28861981f102df263bf07f0faf3b3a3d2efd2a07a85 \
#     -t mobile_world:snapshot-lifecycle-recovery .
ARG BASE_IMAGE=sha256:4e594219474246e1c613a28861981f102df263bf07f0faf3b3a3d2efd2a07a85
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG PARENT_IMAGE_ID=sha256:4e594219474246e1c613a28861981f102df263bf07f0faf3b3a3d2efd2a07a85
ARG PATCH_SOURCE_REVISION=7fb9cc4c5265283b2dd2403d8160c0facae725f6

LABEL org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.base.digest="${PARENT_IMAGE_ID}" \
      org.opencontainers.image.source="https://github.com/pockyitachi/AgentSentinel" \
      org.opencontainers.image.revision="${PATCH_SOURCE_REVISION}" \
      io.agentsentinel.patch.id="snapshot-lifecycle-recovery-v1" \
      io.agentsentinel.patch.source-state="dirty-worktree-byte-pinned" \
      io.agentsentinel.patch.scope="adb-timeouts,snapshot-stability,task-init-recovery,emulator-restart"

COPY *.patch apply_unified_patch.py /tmp/snapshot-lifecycle-recovery/

# The required build command above sets `--network=none`; keep this syntax
# compatible with the host's classic Docker builder as well as BuildKit.
RUN set -eu; \
    printf '%s  %s\n' \
      'f0b2cef69016cb5f3154d10c84cebfbed98c9e03dd2489b2b6a448d525494774' '/app/service/src/mobile_world/runtime/utils/helpers.py' \
      '4fb1b3784b0fc98626f19f7a367a103149b81345bb8d19e481212d3d5eff4d45' '/app/service/src/mobile_world/runtime/controller.py' \
      '8657cc4b6d8fa39b6233be342b89bf8e2de7103853f971c73461ccd324e53d23' '/app/service/src/mobile_world/tasks/base.py' \
      '03b3c1e3223e070fa2ddde0f8aa0bd2e2d3df9ad139ae8bf629404901bbac75b' '/app/service/src/mobile_world/runtime/utils/docker.py' \
      '197813d1ebea1835208a02cc5805be5de21e49fa0d84c382243303445373d3fa' '/app/docker/start_emulator.sh' \
      'afc283f6c17c25b7e0f160cc3a4d459fc63f16d99df8a81a540d0fa948ef0096' '/app/service/src/mobile_world/core/server.py' \
      | sha256sum --check --strict; \
    printf '%s  %s\n' \
      '31d3ec4963894a2ccf02648fbebf4c771f768fa1e3c114e01bad82b6b802d8bb' '/tmp/snapshot-lifecycle-recovery/0001-adb-timeouts.patch' \
      'fc2f5d070b8b8d8b82b68912f814096fb4899897fc9cf13149b7f44988405fdf' '/tmp/snapshot-lifecycle-recovery/0002-snapshot-stability.patch' \
      '9ba065853eeadd4649db34a2e380dcac4706bdf38ee0a48a4aa6735eb782d5b5' '/tmp/snapshot-lifecycle-recovery/0003-task-init-state.patch' \
      'c0b5dd4e8aaea1161e67a61d75cfbc419db094ca7912175b91f3d2c7fc213281' '/tmp/snapshot-lifecycle-recovery/0004-emulator-restart.patch' \
      'fb7f39c02e7ab0c1664fafbd5d11cfe3e3ba771e64f57e1eceaadde2f40e4047' '/tmp/snapshot-lifecycle-recovery/0005-start-emulator.patch' \
      'bdd4af4ff060fb7510547a3b78279b0b4970617ea4c253efba690823d0497638' '/tmp/snapshot-lifecycle-recovery/0006-server-lifecycle.patch' \
      | sha256sum --check --strict; \
    printf '%s  %s\n' \
      '631cc0f0bf060cb4423337f2e2b383c26ffa4afb0ea03f8d2771da8a91c1a13a' '/tmp/snapshot-lifecycle-recovery/apply_unified_patch.py' \
      | sha256sum --check --strict; \
    for patch_file in /tmp/snapshot-lifecycle-recovery/*.patch; do \
      /app/service/.venv/bin/python /tmp/snapshot-lifecycle-recovery/apply_unified_patch.py --root / --check "$patch_file"; \
    done; \
    for patch_file in /tmp/snapshot-lifecycle-recovery/*.patch; do \
      /app/service/.venv/bin/python /tmp/snapshot-lifecycle-recovery/apply_unified_patch.py --root / "$patch_file"; \
    done; \
    chmod 0755 /app/docker/start_emulator.sh; \
    printf '%s  %s\n' \
      'b7e815ef2d51a49b1ea239a3b5bd2a41462e2054294016bf18c6400e7b82fbee' '/app/service/src/mobile_world/runtime/utils/helpers.py' \
      '4d965574e0130aebb4e8df8b5888c804e96fa5089cf9accc6948dccfaf13e634' '/app/service/src/mobile_world/runtime/controller.py' \
      'ffdc99374fe1acac13c02306593890ab1e5f227fb1fd3ed9a6c51aacddc6ee6a' '/app/service/src/mobile_world/tasks/base.py' \
      'b5aefa55894c6287ab424a2470fcd271cd8f37557151b943e2ae675ab6e4004a' '/app/service/src/mobile_world/runtime/utils/docker.py' \
      '453e290e8250823414924dba9d7ae00196e07d2ab07df057e84922ffc469a82e' '/app/docker/start_emulator.sh' \
      'ec054dc0aa3b8fe827aa146100269a80751a5516a815d373ef65ae188ec07d25' '/app/service/src/mobile_world/core/server.py' \
      | sha256sum --check --strict; \
    grep -Fq 'DEFAULT_DEVICE = f"emulator-{_EMULATOR_PORT}"' /app/service/src/mobile_world/core/server.py; \
    grep -Fq '@app.post("/reset")' /app/service/src/mobile_world/core/server.py; \
    grep -Fq '@app.post("/exec")' /app/service/src/mobile_world/core/server.py; \
    ! grep -Fq 'validate_user_agent_environment' /app/service/src/mobile_world/core/server.py; \
    ! grep -Fq '/task-asset/' /app/service/src/mobile_world/core/server.py; \
    ! grep -Fq 'COMMON_APP_MAPPER' /app/service/src/mobile_world/runtime/controller.py; \
    ! grep -Fq '[ASK_USER] Failed to get response' /app/service/src/mobile_world/runtime/controller.py; \
    ! grep -Fq 'def mask_api_key' /app/service/src/mobile_world/runtime/utils/helpers.py; \
    grep -Fq 'def docker_rm(container_name: str, *, force: bool = True) -> None:' /app/service/src/mobile_world/runtime/utils/docker.py; \
    grep -Fq 'def run_task(self, controller: AndroidController, agent_question: str = None)' /app/service/src/mobile_world/tasks/base.py; \
    mkdir -p /tmp/snapshot-lifecycle-recovery/pyc; \
    /app/service/.venv/bin/python -c 'import py_compile; paths=["/app/service/src/mobile_world/runtime/utils/helpers.py","/app/service/src/mobile_world/runtime/controller.py","/app/service/src/mobile_world/tasks/base.py","/app/service/src/mobile_world/runtime/utils/docker.py","/app/service/src/mobile_world/core/server.py"]; [py_compile.compile(path, cfile=f"/tmp/snapshot-lifecycle-recovery/pyc/{index}.pyc", doraise=True) for index, path in enumerate(paths)]'; \
    /bin/bash -n /app/docker/start_emulator.sh; \
    rm -rf /tmp/snapshot-lifecycle-recovery
