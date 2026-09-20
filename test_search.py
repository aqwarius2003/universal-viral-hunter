import asyncio
import json
from twikit import Client

async def main():
    with open('cookies.json', 'r') as f:
        cookies = json.load(f)
    
    client = Client(language="en-US")
    client.set_cookies(cookies)
    
    print("Testing search_tweet...")
    try:
        results = await client.search_tweet("(AI OR LLM OR 'machine learning') lang:en -is:retweet", product="Top", count=10)
        print("Results found:", len(results) if results else 0)
        for tweet in results:
            print(" -", getattr(tweet, "id", None))
    except Exception as e:
        print("Error during search:", type(e).__name__, str(e))
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
