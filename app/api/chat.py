import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db import get_db
from app.models import ChatMessage, Conversation, User
from app.schemas import ChatRequest, ChatResponse
from app.services.ai import complete

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> ChatResponse:
    if payload.conversation_id is None:
        conversation = Conversation(user_id=user.id, title=payload.message[:80])
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    else:
        conversation = db.scalar(select(Conversation).where(Conversation.id == payload.conversation_id, Conversation.user_id == user.id))
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
    image_urls = [str(url) for url in payload.image_urls]
    db.add(ChatMessage(conversation_id=conversation.id, role="user", content=payload.message, image_urls=image_urls))
    db.commit()
    history = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id).order_by(ChatMessage.created_at.desc()).limit(20)).all()
    messages = []
    for item in reversed(history):
        if item.role == "user" and item.image_urls:
            content = [{"type": "text", "text": item.content}]
            content.extend({"type": "image_url", "image_url": {"url": image_url}} for image_url in item.image_urls)
        else:
            content = item.content
        messages.append({"role": item.role, "content": content})
    db.commit()
    try:
        reply = await complete(messages)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="AI service is not configured") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="AI service request failed") from exc
    db.add(ChatMessage(conversation_id=conversation.id, role="assistant", content=reply))
    db.commit()
    return ChatResponse(conversation_id=conversation.id, reply=reply)


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, object]]:
    conversations = db.scalars(select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.created_at.desc())).all()
    return [{"id": item.id, "title": item.title, "created_at": item.created_at} for item in conversations]


@router.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict[str, object]]:
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return [{"id": item.id, "role": item.role, "content": item.content, "image_urls": item.image_urls, "created_at": item.created_at} for item in conversation.messages]
