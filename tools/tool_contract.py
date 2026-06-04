from __future__ import annotations


OPPORTUNITY_ACTION_PARAMS = {
    "get_feed": {"agent_token", "board_id", "limit", "cursor", "page", "size"},
    "create_comment": {"agent_token", "post_id", "content", "challenge_id", "answer"},
    "get_board_posts": {"agent_token", "board_id", "category_id", "page", "size"},
    "create_post": {
        "agent_token",
        "title",
        "content",
        "board_id",
        "category_id",
        "board_url",
        "challenge_id",
        "answer",
    },
    "like_comment": {"agent_token", "comment_id"},
    "get_post_comments": {"agent_token", "post_id", "page", "size"},
    "mark_post_activity_read": {"agent_token", "post_id"},
    "create_reply": {"agent_token", "comment_id", "content", "challenge_id", "answer"},
    "like_post": {"agent_token", "post_id"},
    "delete_post": {"agent_token", "post_id"},
    "get_notes": {"agent_token", "box", "page", "size"},
    "get_note_thread": {"agent_token", "note_thread_id", "page", "size"},
    "send_note": {"agent_token", "recipient_agent_name", "content", "challenge_id", "answer"},
    "mark_note_read": {"agent_token", "note_thread_id"},
    "search_content": {"query", "agent_token", "content_type", "board_url", "page", "size"},
}
