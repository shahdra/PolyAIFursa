from langchain.chat_models import init_chat_model

model = init_chat_model(
    "amazon.nova-lite-v1:0",
    model_provider="bedrock",
    region_name="us-east-1",
)

response = model.invoke("Hello! Reply in one short sentence.")
print(response.content)
