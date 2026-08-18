# https://spec.matrix.org/v1.11/client-server-api/#mroommember
EVENT_TYPE_M_ROOM_MEMBER = "m.room.member"
MEMBERSHIP_CONTENT_KEY = "membership"
MEMBERSHIP_JOIN = "join"
MEMBERSHIP_KNOCK = "knock"

# https://spec.matrix.org/v1.11/client-server-api/#mroompower_levels
EVENT_TYPE_M_ROOM_POWER_LEVELS = "m.room.power_levels"
USERS_POWER_LEVEL_KEY = "users"
USERS_DEFAULT_POWER_LEVEL_KEY = "users_default"

# https://spec.matrix.org/v1.11/client-server-api/#mroomcreate
EVENT_TYPE_M_ROOM_CREATE = "m.room.create"

# The client treats power level >= 100 as "admin" — the people who can accept
# or deny knocks. Only their block lists gate entry.
ADMIN_POWER_LEVEL = 100
