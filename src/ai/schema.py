from typing import Optional, List
from pydantic import BaseModel, Field

class TweetSummary(BaseModel):
    title_ru: str = Field(description="Title in Russian, max 10 words")
    summary_ru: str = Field(description="Summary in Russian, 2-3 sentences")
    takeaways: List[str] = Field(
        description="Exactly 3 bullet points in Russian", 
        min_length=3, 
        max_length=3
    )
    impact_score: int = Field(ge=1, le=10, description="Score from 1 to 10")
    impact_reason: str = Field(description="Brief reason for the score in Russian")
    hashtags: List[str] = Field(
        description="3 to 5 relevant hashtags, each strictly starting with # (e.g. #AI, #Tech, #Кибербезопасность)"
    )
    selected_image_index: Optional[int] = Field(
        default=None,
        description="0-based index of the most informative/relevant image from the attached images list, or null if images are irrelevant/low quality or none attached"
    )
    image_reason: Optional[str] = Field(
        default=None,
        description="Brief explanation in Russian why this image was selected or why null was returned"
    )

class ImageSelection(BaseModel):
    selected_image_index: Optional[int] = Field(
        default=None,
        description="0-based index of the most informative image from the attached images list, or null if none are informative"
    )
    image_reason: Optional[str] = Field(
        default=None,
        description="Brief explanation in Russian why this image was selected or why null was returned"
    )

class DigestHighlightItem(BaseModel):
    title_ru: str = Field(description="Headline or keywords in Russian for this news item (e.g. 'Релиз GPT-5' or 'Новая архитектура трансформеров')")
    summary_ru: str = Field(description="1-2 sentences summarizing key details and impact in Russian")
    source_url: str = Field(description="Original X.com tweet URL from the provided context corresponding to this news")


class DailyDigestSummary(BaseModel):
    main_headline_ru: str = Field(description="Catchy headline in Russian for the top AI story of the day")
    main_story_summary_ru: str = Field(description="Detailed summary in Russian of the main AI breakthrough or top news (2-4 sentences)")
    main_story_url: Optional[str] = Field(default="", description="Original X.com tweet URL for the main story")
    key_highlights: Optional[List[DigestHighlightItem]] = Field(
        default=None,
        description="2 to 5 structured news highlights with Russian title, summary, and exact source URL",
    )
    key_highlights_ru: Optional[List[str]] = Field(
        default=None,
        description="Optional list of key highlight strings in Russian",
    )
    takeaways_ru: List[str] = Field(
        description="2 to 3 strategic conclusions or industry trends in Russian",
        min_length=2,
        max_length=3
    )
    hashtags: List[str] = Field(
        description="3 to 5 relevant hashtags including #AIDigest, each strictly starting with #"
    )