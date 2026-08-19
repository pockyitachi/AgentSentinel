# Upstream source provenance

## MobileWorld

- Upstream repository: `https://github.com/Tongyi-MAI/MobileWorld.git`
- Upstream branch at import: `main`
- Imported commit: `0dcd0980eac64d76f498f93568a1ec0594b743c4`
- Import policy: source snapshot incorporated into this monorepo under
  `MobileWorld/`; upstream is read-only provenance, not a push destination.
- Upstream license: Apache License 2.0, preserved at `MobileWorld/LICENSE`.

Collector and audit changes made under `MobileWorld/` belong to the
AgentSentinel repository. They do not change the upstream MobileWorld project
unless a future maintainer deliberately creates a separate upstream
contribution.

The snapshot references these upstream resource submodules:

| Path | Repository | Pinned commit |
|---|---|---|
| `MobileWorld/resources/mail` | `https://github.com/nrgao/mail_fork.git` | `545355b4feab53893c30ea968036d6800a4006a0` |
| `MobileWorld/resources/mall` | `https://github.com/qykong/mall_fork.git` | `a2f25aaf3907946ebc8a093bfab1f4c974675eff` |
| `MobileWorld/resources/mastodon-android` | `https://github.com/patdooog/mastodon-android.git` | `9a28bb3f1c4bcea90cf6facd1efb657198e9ac84` |

