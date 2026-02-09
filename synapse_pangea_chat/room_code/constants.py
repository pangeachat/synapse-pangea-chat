# https://spec.matrix.org/v1.11/client-server-api/#mroomjoin_rules
EVENT_TYPE_M_ROOM_JOIN_RULES = "m.room.join_rules"
JOIN_RULE_CONTENT_KEY = "join_rule"
KNOCK_JOIN_RULE_VALUE = "knock"  # Existing join rule value
ACCESS_CODE_JOIN_RULE_CONTENT_KEY = "access_code"  # New join rule content key

# https://spec.matrix.org/v1.11/client-server-api/#mroommember
EVENT_TYPE_M_ROOM_MEMBER = "m.room.member"
ACCESS_CODE_KNOCK_EVENT_CONTENT_KEY = "access_code"  # New knock event content key
MEMBERSHIP_CONTENT_KEY = "membership"  # existing membership content key
MEMBERSHIP_KNOCK = "knock"  # existing membership value
MEMBERSHIP_INVITE = "invite"  # existing membership value
MEMBERSHIP_JOIN = "join"  # existing membership value

# https://spec.matrix.org/v1.11/client-server-api/#mroompower_levels
EVENT_TYPE_M_ROOM_POWER_LEVELS = "m.room.power_levels"
INVITE_POWER_LEVEL_KEY = "invite"  # existing power level key
DEFAULT_INVITE_POWER_LEVEL = 0  # existing power level value
USERS_DEFAULT_POWER_LEVEL_KEY = "users_default"  # existing power level key
DEFAULT_USERS_DEFAULT_POWER_LEVEL = 0  # existing power level value
USERS_POWER_LEVEL_KEY = "users"  # existing power level key
