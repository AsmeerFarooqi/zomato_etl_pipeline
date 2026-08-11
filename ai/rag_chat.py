import os
import numpy as np
import pandas as pd
import streamlit as st
import snowflake.connector
import requests
from dotenv import load_dotenv

load_dotenv()

EMBEDDING_MODEL = "nomic-embed-text"
CHAT_MODEL = "llama3.2:3b"
NEW_REVIEWS = 500
TOK_K = 5

# Local cache for Ollama embeddings
CACHE_FILE = "review_embeddings_ollama.parquet"

OLLAMA_URL = "http://localhost:11434"


# ============================================================
# SNOWFLAKE
# ============================================================

def read_reviews_from_snowflake():

    conn = snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA"),
    )

    query = f"""
        SELECT REVIEW_ID, CITY, RATING, COMMENT
        FROM ZOMATO.STAGING.STG_REVIEWS
        SAMPLE ({NEW_REVIEWS} ROWS)
    """

    try:
        df = conn.cursor().execute(query).fetch_pandas_all()
    finally:
        conn.close()

    df.columns = [col.lower() for col in df.columns]

    # Make sure comments are strings
    df["comment"] = df["comment"].fillna("").astype(str)

    return df


# ============================================================
# OLLAMA EMBEDDINGS
# ============================================================

def embed(texts):

    embeddings = []

    for text in texts:

        text = "" if text is None else str(text)

        response = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={
                "model": EMBEDDING_MODEL,
                "prompt": text
            },
            timeout=120
        )

        if response.status_code != 200:
            raise Exception(
                f"Ollama embedding error: "
                f"{response.status_code} - "
                f"{response.text}"
            )

        data = response.json()

        embeddings.append(data["embedding"])

    return embeddings

# ============================================================
# LOAD REVIEWS
# ============================================================

@st.cache_data()
def load_reviews():

    # Check if local Ollama embeddings already exist
    if os.path.exists(CACHE_FILE):

        st.info(
            "Loading reviews and embeddings from local cache..."
        )

        df = pd.read_parquet(CACHE_FILE)

        return df

    # --------------------------------------------------------
    # Step 1: Get reviews from Snowflake
    # --------------------------------------------------------

    st.info(
        f"Loading {NEW_REVIEWS} reviews from Snowflake..."
    )

    df = read_reviews_from_snowflake()

    # --------------------------------------------------------
    # Step 2: Create local embeddings
    # --------------------------------------------------------

    st.info(
        f"Creating embeddings locally using "
        f"{EMBEDDING_MODEL}..."
    )

    embeddings = embed(
        df["comment"].tolist()
    )

    df["embedding"] = embeddings

    # --------------------------------------------------------
    # Step 3: Save embeddings locally
    # --------------------------------------------------------

    df.to_parquet(CACHE_FILE)

    st.success(
        f"Successfully created embeddings for "
        f"{len(df)} reviews."
    )

    return df


# ============================================================
# STREAMLIT UI
# ============================================================

st.title("Chat with your Zomato Reviews")

st.caption(
    f"Searching {NEW_REVIEWS} reviews, "
    f"answering with local {CHAT_MODEL} model"
)


# ============================================================
# LOAD REVIEW DATA
# ============================================================

try:

    review_df = load_reviews()

    st.success(
        f"{len(review_df)} reviews loaded successfully."
    )

except Exception as e:

    st.error(
        "There was an error while loading the reviews."
    )

    st.exception(e)

    st.stop()


# ============================================================
# COSINE SIMILARITY
# ============================================================

def consine_simiarity(vec_a, vec_b):

    vec_a = np.array(vec_a)
    vec_b = np.array(vec_b)

    denominator = (
        np.linalg.norm(vec_a)
        * np.linalg.norm(vec_b)
    )

    if denominator == 0:
        return 0

    return np.dot(vec_a, vec_b) / denominator


# ============================================================
# FIND SIMILAR REVIEWS
# ============================================================

def find_similar_reviews(question, df):

    # Create embedding for user's question
    question_vector = embed([question])[0]

    scores = []

    # Compare question with every review
    for review_vector in df["embedding"]:

        scores.append(
            consine_simiarity(
                question_vector,
                review_vector
            )
        )

    df = df.copy()

    df["score"] = scores

    # Get top 5 most similar reviews
    return df.nlargest(TOK_K, "score")


# ============================================================
# ASK LOCAL LLM
# ============================================================

def ask_llm(question, top_reviews):

    context = ""

    # Build context from Top 5 reviews
    for _, row in top_reviews.iterrows():

        context += (
            f"({row['city']}, "
            f"{row['rating']} stars) "
            f"{row['comment']}\n"
        )

    system_prompt = (
        "Answer ONLY using the customer reviews provided. "
        "Be concise. "
        "If the reviews don't cover it, say so."
    )

    user_prompt = (
        f"Question: {question}\n\n"
        f"Reviews:\n{context}"
    )

    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": CHAT_MODEL,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],
            "stream": False
        },
        timeout=300
    )

    response.raise_for_status()

    data = response.json()

    return data["message"]["content"]


# ============================================================
# USER QUESTION
# ============================================================

question = st.text_input(
    "Ask a question about your reviews:",
    placeholder=(
        "e.g. What are the most common complaints "
        "about delivery?"
    )
)


# ============================================================
# RAG PIPELINE
# ============================================================

if question:

    try:

        with st.spinner(
            "Finding relevant customer reviews..."
        ):

            # Retrieve Top 5 similar reviews
            top_reviews = find_similar_reviews(
                question,
                review_df
            )

        with st.spinner(
            "Generating answer using local Llama..."
        ):

            # Send Top 5 reviews to local LLM
            answer = ask_llm(
                question,
                top_reviews
            )

        # Display answer
        st.markdown("**Answer:**")

        st.write(answer)

        # Display reviews used by LLM
        with st.expander(
            "Reviews used to build this answer"
        ):

            st.dataframe(
                top_reviews[
                    ["city", "rating", "comment"]
                ],
                hide_index=True
            )

    except Exception as e:

        st.error(
            "Something went wrong while processing "
            "your question."
        )

        st.exception(e)