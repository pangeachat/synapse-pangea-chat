# https://spec.matrix.org/v1.11/client-server-api/#mroommember
EVENT_TYPE_M_ROOM_MEMBER = "m.room.member"
MEMBERSHIP_CONTENT_KEY = "membership"  # existing membership content key
MEMBERSHIP_INVITE = "invite"  # existing membership value
MEMBERSHIP_JOIN = "join"  # existing membership value
MEMBERSHIP_LEAVE = "leave"  # existing membership valu

# Marker added to the self-leave events sent on room deletion so clients can
# distinguish "the room was deleted" from a voluntary leave (client#8237)
ROOM_DELETED_CONTENT_KEY = "pangea.room_deleted"
ROOM_DELETED_REASON = "This space has been deleted"

# https://spec.matrix.org/v1.11/client-server-api/#mroompower_levels
EVENT_TYPE_M_ROOM_POWER_LEVELS = "m.room.power_levels"

# Space-related event types
# https://spec.matrix.org/v1.11/client-server-api/#mspacechild
EVENT_TYPE_M_SPACE_CHILD = "m.space.child"
# https://spec.matrix.org/v1.11/client-server-api/#mspaceparent
EVENT_TYPE_M_SPACE_PARENT = "m.space.parent"
