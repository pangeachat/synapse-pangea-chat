# https://spec.matrix.org/v1.11/client-server-api/#mroomjoin_rules
EVENT_TYPE_M_ROOM_JOIN_RULES = "m.room.join_rules"
JOIN_RULE_CONTENT_KEY = "join_rule"
KNOCK_JOIN_RULE_VALUE = "knock"  # Existing join rule value
ACCESS_CODE_JOIN_RULE_CONTENT_KEY = "access_code"  # New join rule content key
ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY = (
    "admin_access_code"  # Admin join rule content key (single-use)
)

# https://spec.matrix.org/v1.11/client-server-api/#mroommember
EVENT_TYPE_M_ROOM_MEMBER = "m.room.member"
ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY = "access_code"  # New knock event content key
MEMBERSHIP_CONTENT_KEY = "membership"  # existing membership content key
MEMBERSHIP_KNOCK = "knock"  # existing membership value
MEMBERSHIP_INVITE = "invite"  # existing membership value
MEMBERSHIP_JOIN = "join"  # existing membership value
MEMBERSHIP_BAN = "ban"  # existing membership value

# Pangea-custom errcode: the presented code matched room(s) the user is
# banned from. Lets the client show a ban-specific message instead of the
# generic "code doesn't exist" failure (issue #127 / client#6820).
ERRCODE_BANNED_FROM_ROOM = "ORG.PANGEA.BANNED_FROM_ROOM"

# Pangea-custom errcode: a well-formed code matched no room. Paired with a
# 404 so clients can tell "the code doesn't exist" from a malformed request
# (issue #197 / client#8693).
ERRCODE_CODE_NOT_FOUND = "ORG.PANGEA.CODE_NOT_FOUND"

# Pangea-custom errcode: the code matched room(s) but every server-side
# invite failed — nothing was joined, nothing was invited (issue #197).
ERRCODE_INVITE_FAILED = "ORG.PANGEA.INVITE_FAILED"

# https://spec.matrix.org/v1.11/client-server-api/#mroompower_levels
EVENT_TYPE_M_ROOM_POWER_LEVELS = "m.room.power_levels"
INVITE_POWER_LEVEL_KEY = "invite"  # existing power level key
DEFAULT_INVITE_POWER_LEVEL = 0  # existing power level value
USERS_DEFAULT_POWER_LEVEL_KEY = "users_default"  # existing power level key
DEFAULT_USERS_DEFAULT_POWER_LEVEL = 0  # existing power level value
USERS_POWER_LEVEL_KEY = "users"  # existing power level key
