import asyncio
import time
from datetime import datetime, timezone
from langgraph.graph import StateGraph, START, END
from src.ai.agents.state import AgentState
from src.ai.agents.scout import scout_node
from src.ai.agents.analyst import analyst_node
from src.ai.agents.editor import editor_node
from src.ai.agents.critic import critic_node, should_publish
from src.ai.agents.publisher import publisher_node
from src.db import init_db, record_pipeline_run

async def run_pipeline():
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()
    status = "SUCCESS"
    error_message = None
    tweets_found = 0
    viral_found = 0

    try:
        # 1. Инициализация БД
        print("Инициализация базы данных...")
        await init_db()

        # 2. Сборка графа
        workflow = StateGraph(AgentState)

        workflow.add_node("scout", scout_node)
        workflow.add_node("analyst", analyst_node)
        workflow.add_node("editor", editor_node)
        workflow.add_node("critic", critic_node)
        workflow.add_node("publisher", publisher_node)

        # Линейный поток
        workflow.add_edge(START, "scout")
        workflow.add_edge("scout", "analyst")
        workflow.add_edge("analyst", "editor")
        workflow.add_edge("editor", "critic")

        # Условный переход (Цикл рефлексии)
        workflow.add_conditional_edges("critic", should_publish)

        # Завершение
        workflow.add_edge("publisher", END)

        app = workflow.compile()

        # 3. Запуск графа
        print("Запуск пайплайна Viral Hunter...")
        
        # Передаем пустое начальное состояние
        initial_state = {
            "raw_tweets": [], 
            "viral_tweets": [], 
            "draft_posts": [], 
            "validation_errors": [], 
            "ready_to_publish": []
        }
        
        final_state = await app.ainvoke(initial_state)

        raw_tweets = final_state.get("raw_tweets", [])
        viral_tweets = final_state.get("viral_tweets", [])
        tweets_found = len(raw_tweets)
        viral_found = len(viral_tweets)

        # 4. Вывод результатов в консоль (Имитация отправки в Telegram)
        posts = final_state.get('ready_to_publish', [])
        print(f"\nПайплайн завершен! Готово к публикации: {len(posts)} постов.\n")
        
        for post in posts:
            print("="*50)
            if post.get("photo_url"):
                print(f"[Attached Photo]: {post['photo_url']}")
            print(post["text"])
            print("-" * 50)
            print(f"[Inline Кнопка]: 🔗 Читать оригинал в X -> {post['tweet_url']}")
            print("="*50 + "\n")

        return final_state

    except Exception as e:
        status = "ERROR"
        error_message = str(e)
        raise e
    finally:
        finished_at = datetime.now(timezone.utc).isoformat()
        duration_sec = round(time.time() - start_time, 2)
        try:
            await record_pipeline_run(
                started_at=started_at,
                finished_at=finished_at,
                duration_sec=duration_sec,
                tweets_found=tweets_found,
                viral_found=viral_found,
                status=status,
                error_message=error_message,
            )
        except Exception as rec_err:
            print(f"⚠️ Ошибка сохранения записи pipeline_runs: {rec_err}")

if __name__ == "__main__":
    asyncio.run(run_pipeline())