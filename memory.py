conversation_memory = {}

MAX_HISTORY = 10


def get_history(session_id):
    return conversation_memory.get(session_id, [])


def add_message(session_id, role, content):

    if session_id not in conversation_memory:
        conversation_memory[session_id] = []

    conversation_memory[session_id].append(
        {
            "role": role,
            "content": content
        }
    )

    conversation_memory[session_id] = conversation_memory[session_id][-MAX_HISTORY:]


def clear_history(session_id):

    if session_id in conversation_memory:
        del conversation_memory[session_id]