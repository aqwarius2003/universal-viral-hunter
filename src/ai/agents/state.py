from typing import TypedDict, Annotated
import operator

class AgentState(TypedDict):
    # Данные из X.com (Scout)
    raw_tweets: list[dict]
    
    # Твиты, прошедшие порог виральности (Analyst)
    viral_tweets: list[dict]
    
    # Сгенерированные JSON-черновики (Editor)
    draft_posts: list[dict]
    
    # Ошибки валидации от Critic (используем Annotated + operator.add для накопления в цикле)
    validation_errors: Annotated[list[str], operator.add]
    
    # Итоговые тексты для публикации
    ready_to_publish: list[str]