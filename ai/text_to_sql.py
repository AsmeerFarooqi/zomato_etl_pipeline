import os
import pandas as pd
import streamlit as st
import snowflake.connector
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Local Ollama model
# ============================================================

CHAT_MODEL = "llama3.2:3b"
OLLAMA_URL = "http://localhost:11434"

FORBIDDEN_WORDS = [
    "drop",
    "delete",
    "truncate",
    "alter",
    "update",
    "insert",
    "create",
    "replace",
    "grant",
    "revoke"
]

EXAMPLE_QUESTIONS = [
    "Top 10 cities by GMV",
    "Which cuisine has the most orders?",
    "Average delivery time by city, worst first",
    "Cancel rate by payment method"
]

# ============================================================
# Snowflake schema information for Ollama
# ============================================================

SCHEMA = """
Database: ZOMATO
Schema: MARTS

Use these EXACT table names and columns.

FACT_ORDERS(
    ORDER_ID,
    ORDER_TIMESTAMP,
    ORDER_DATE,
    CUSTOMER_ID,
    RESTAURANT_ID,
    CITY,
    CUISINE,
    PAYMENT_METHOD,
    ORDER_STATUS,
    IS_DELIVERED,
    ITEMS_COUNT,
    SALES_QTY,
    SUBTOTAL,
    DISCOUNT,
    DELIVERY_FEE,
    GST,
    SALES_AMOUNT,
    CUSTOMER_RATING,
    DELIVERY_TIME_MIN
)

DIM_CUSTOMER(
    CUSTOMER_ID,
    CUSTOMER_NAME,
    AGE,
    AGE_SEGMENT,
    GENDER,
    CITY
)

DIM_FOOD(
    FOOD_ID,
    FOOD_NAME,
    CUISINE,
    CATEGORY
)

DIM_RESTAURANTS(
    RESTAURANT_ID,
    RESTAURANT_NAME,
    CITY,
    CUISINE,
    RATING,
    COST_FOR_TWO
)

MART_DAILY_CITY_REVENUNE(
    ORDER_DATE,
    CITY,
    ORDERS,
    CANCEL_RATE,
    GMV,
    AOV
)

MART_RESTAURANT_PERFORMANCE(
    RESTAURANT_ID,
    RESTAURANT_NAME,
    CITY,
    CUISINE,
    ORDERS,
    REVENUE,
    AVG_CUSTOMER_RATING,
    CANCEL_RATE
)

MART_DELIVERY_SLA(
    CITY,
    ORDER_HOUR,
    DELIVERED_ORDERS,
    P50,
    p90,
   
)

IMPORTANT:

1. FACT_ORDERS does NOT have an ORDERS column.
2. To count orders in FACT_ORDERS, use COUNT(*).
3. For "most orders" use COUNT(*) with FACT_ORDERS.
4. MART_RESTAURANT_PERFORMANCE has an ORDERS column.
5. MART_DAILY_CITY_REVENUNE has an ORDERS column.
6. FACT_ORDERS uses SALES_AMOUNT for revenue.
7. FACT_ORDERS uses DELIVERY_TIME_MIN for delivery time.
8. FACT_ORDERS uses IS_DELIVERED to identify delivered orders.
9. GMV means delivered revenue.
10. Prefer MART tables when they directly answer the question.
11. DIM_FOOD has CUISINE, but use FACT_ORDERS.CUISINE when the question asks
    which cuisine has the most orders.
12. Never invent columns.
13. Never use columns that are not listed above.
"""

SYSTEM_PROMPT = f"""
You are a Snowflake SQL expert.

Your job is to convert the user's English question into ONE valid
Snowflake SELECT query.

Rules:

- Generate ONLY SELECT or WITH queries.
- Never modify data.
- Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE,
  GRANT, or REVOKE.
- Use the exact table and column names provided in the schema.
- The current database is ZOMATO.
- The current schema is MARTS.
- You can use bare table names.
- Do NOT invent columns.
- If the question asks "most orders" from FACT_ORDERS,
  use COUNT(*), NOT ORDERS.
- If the question asks about delivery time,
  use DELIVERY_TIME_MIN from FACT_ORDERS.
- If the question asks about GMV,
  use GMV from MART_DAILY_CITY_REVENUNE when appropriate,
  or calculate delivered revenue from FACT_ORDERS.
- Prefer MART tables when they directly answer the question.
- Add LIMIT 100 or less for lists.
- If the question asks for TOP 10, use LIMIT 10.
- Return exactly one SQL query.
- Return JSON only.

Required JSON format:

{{"sql": "SELECT ..."}}

Schema:

{SCHEMA}
"""

# ============================================================
# Snowflake connection
# ============================================================

@st.cache_resource
def get_connection():

    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database="ZOMATO",
        schema="MARTS",
        role="DBT_ROLE"
    )


# ============================================================
# Generate SQL using local Ollama
# ============================================================

def generate_sql(question):

    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": CHAT_MODEL,
            "temperature": 0,
            "format": "json",
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": question
                }
            ]
        },
        timeout=120
    )

    response.raise_for_status()

    data = response.json()

    answer = data["message"]["content"]

    result = json.loads(answer)

    sql = result["sql"]

    # Remove database/schema prefix if Ollama adds it
    sql = sql.replace(
        "ZOMATO.MARTS.",
        ""
    )

    sql = sql.replace(
        "ZOMATO.",
        ""
    )

    return sql.strip().rstrip(";")


# ============================================================
# SQL safety check
# ============================================================

def is_safe(sql):

    lowered = sql.lower().strip()

    # Query must start with SELECT or WITH
    if not (
        lowered.startswith("select")
        or lowered.startswith("with")
    ):
        return False

    # Block dangerous SQL keywords
    for word in FORBIDDEN_WORDS:

        if word in lowered:
            return False

    return True


# ============================================================
# Run query in Snowflake
# ============================================================

def run_query(sql):

    conn = get_connection()

    cursor = conn.cursor()

    try:

        return cursor.execute(sql).fetch_pandas_all()

    finally:

        cursor.close()


# ============================================================
# Streamlit UI
# ============================================================

st.title("Chat with your Zomato Data")

st.caption(
    f"Ask in English, {CHAT_MODEL} writes the SQL, "
    "Snowflake runs it"
)


# ============================================================
# Sidebar
# ============================================================

with st.sidebar:

    st.header("Example Questions")

    for q in EXAMPLE_QUESTIONS:

        st.markdown(f"- {q}")


# ============================================================
# User question
# ============================================================

question = st.text_input(
    "Enter your question here",
    placeholder="e.g. Top 10 restaurants by revenue in Bangalore"
)


# ============================================================
# Process question
# ============================================================

if question:

    try:

        # ----------------------------------------------------
        # Generate SQL using Ollama
        # ----------------------------------------------------

        sql = generate_sql(question)

        st.subheader("Generated SQL")

        st.code(
            sql,
            language="sql"
        )

        # ----------------------------------------------------
        # Safety check
        # ----------------------------------------------------

        if not is_safe(sql):

            st.error(
                "The generated SQL is not safe to run."
            )

        else:

            try:

                # ------------------------------------------------
                # Execute SQL in Snowflake
                # ------------------------------------------------

                df = run_query(sql)

                st.success(
                    f"{len(df)} rows returned"
                )

                # ------------------------------------------------
                # Display result
                # ------------------------------------------------

                st.dataframe(
                    df,
                    hide_index=True
                )

                # ------------------------------------------------
                # Display chart for 2-column numeric result
                # ------------------------------------------------

                if (
                    len(df.columns) == 2
                    and pd.api.types.is_numeric_dtype(
                        df.iloc[:, 1]
                    )
                ):

                    st.subheader("Visualization")

                    st.bar_chart(
                        df,
                        x=df.columns[0],
                        y=df.columns[1]
                    )

            except Exception as e:

                st.error(
                    f"Error running query: {e}"
                )

    except Exception as e:

        st.error(
            f"Error generating SQL with Ollama: {e}"
        )