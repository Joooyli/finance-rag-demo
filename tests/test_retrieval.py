import json

from app import rag_pipeline


def test_retrieve_option_value_factors():
    question = "期权价值会受到什么因素影响"
    result = rag_pipeline.retrieve(question, top_k=3)

    print(json.dumps({"question": question, "results": result}, ensure_ascii=False, indent=2))

