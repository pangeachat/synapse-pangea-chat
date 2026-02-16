from synapse.module_api import ModuleApi


async def unassigned_activity_role_on_leave(
    room_id: str, leave_user_id: str, api: ModuleApi
):
    ...
