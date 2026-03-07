---
applyTo: "synapse_pangea_chat/limit_user_directory/**,synapse_pangea_chat/user_directory_search/**,tests/test_limit_user_directory*,tests/test_user_directory_search*"
---

# Limit User Directory & Custom Search Endpoint

Two complementary mechanisms for controlling user directory visibility:

1. **Spam checker callback** (`limit_user_directory/`) — post-filters Synapse's built-in search results.
2. **Custom search endpoint** (`user_directory_search/`) — replaces the built-in search by pushing all filtering into a single SQL query, so LIMIT applies *after* filtering.

## Filtering Logic (Decision Order)

Both mechanisms implement identical visibility rules. The **first match wins**:

1. **Whitelisted requester** → include (bypass all checks)
   - If `requester_id` matches any pattern in `limit_user_directory_whitelist_requester_id_patterns`, include the user.

2. **Remote user** → include
   - If the candidate `user_id` is not local (not on this homeserver), include them.

3. **Resolve public attribute** from account data at `limit_user_directory_public_attribute_search_path` (dot-syntax, e.g. `profile.user_settings.public`):
   - If the account data key is missing or the nested path doesn't resolve → treat as "missing". Return `limit_user_directory_filter_search_if_missing_public_attribute` (default: `True` = exclude).
   - If the resolved value is a boolean `true` or string `"true"` (case-insensitive) → **public**.
   - Otherwise → **not public**.

4. **Public user** → include (bypass room-sharing check)
   - If the user is public, they appear in search results for all requesters. No room-sharing check needed.

5. **Not-public user: check shared rooms** → include only if requester shares at least one room (private or public) with the candidate.

6. **Default** → exclude

## Custom Search Endpoint

`POST /_synapse/client/pangea/v1/user_directory/search`

### Request body

```json
{ "search_term": "alice", "limit": 10 }
```

- `search_term` (string, required): text to match against user names/IDs.
- `limit` (int, optional, default 10, max 50): number of results to return.

### Response

```json
{
  "limited": false,
  "results": [
    { "user_id": "@alice:example.com", "display_name": "Alice", "avatar_url": "mxc://..." }
  ]
}
```

### Why it exists

Synapse's built-in `/_matrix/client/v3/user_directory/search` fetches a fixed batch of users (LIMIT + 1) from the DB, then post-filters with the spam checker. If many users are filtered out, the response contains fewer results than `limit` — Synapse never compensates by fetching more. The custom endpoint pushes all visibility logic into SQL so the final LIMIT applies after filtering.

## TODO: Phase Out Legacy Logic

- [ ] Migrate all client call sites from `/_matrix/client/v3/user_directory/search` to `/_synapse/client/pangea/v1/user_directory/search`.
- [ ] Add observability (request counters) to verify legacy built-in path traffic is effectively zero.
- [ ] Remove `LimitUserDirectory` registration from `PangeaChat.__init__`.
- [ ] Delete `synapse_pangea_chat/limit_user_directory/` implementation and legacy tests after traffic cutover.
- [ ] Keep one parity test that confirms custom endpoint behavior for public/private/shared-room visibility.

### SQL approach

A single query joins `user_directory_search` (tsvector match) → `user_directory` → LEFT JOIN `account_data` (public attribute extraction via `::jsonb`), with WHERE conditions for the visibility rules and EXISTS subqueries for shared-room checks. The ranking formula mirrors Synapse's `ts_rank_cd` approach.

The query shape should stay aligned with Synapse's built-in implementation in `synapse/storage/databases/main/user_directory.py` (`UserDirectoryStore.search_user_dir`), especially for ranking semantics and locked-user filtering behavior.

## Config

All config keys live under the module's `config` block in `homeserver.yaml`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `limit_user_directory_public_attribute_search_path` | `str \| null` | `null` | Dot-syntax path to the public flag in account data. If `null`, both the spam checker and the custom endpoint are disabled. |
| `limit_user_directory_whitelist_requester_id_patterns` | `list[str]` | `[]` | Regex patterns for requester user IDs that bypass all filtering. |
| `limit_user_directory_filter_search_if_missing_public_attribute` | `bool` | `true` | Whether to exclude users whose public attribute is missing. |
| `user_directory_search_requests_per_burst` | `int` | `10` | Max requests per rate-limit window for the custom endpoint. |
| `user_directory_search_burst_duration_seconds` | `int` | `60` | Rate-limit window duration in seconds. |

## Key Files

- **Spam checker**: `synapse_pangea_chat/limit_user_directory/__init__.py`
- **Custom endpoint**: `synapse_pangea_chat/user_directory_search/user_directory_search.py`
- **SQL query logic**: `synapse_pangea_chat/user_directory_search/search_users.py`
- **Rate limiter**: `synapse_pangea_chat/user_directory_search/is_rate_limited.py`
- **Registration**: `synapse_pangea_chat/__init__.py` (both enabled only if `public_attribute_search_path` is set)
- **Config**: `synapse_pangea_chat/config.py`
- **Integration tests (spam checker)**: `tests/test_limit_user_directory_e2e.py`
- **Unit tests (spam checker)**: `tests/test_limit_user_directory_unit.py`
- **Integration tests (custom endpoint)**: `tests/test_user_directory_search_e2e.py`
