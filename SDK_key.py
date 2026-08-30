from google import genai

client = genai.Client(api_key="AQ.Ab8RN6LpkQbJv7C7efi6G_Wpdo2b6xDB-yUUc-ZnIk3iBus41w")
response = client.models.generate_content(
    model="gemini-3.5-flash",
    contents="Hello",
)
print(response.text)
