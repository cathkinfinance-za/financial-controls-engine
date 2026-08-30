import os
import google.generativeai as genai

genai.configure(api_key="AQ.Ab8RN6LpkQbJv7C7efi6G_Wpdo2b6xDB-yUUc-ZnIk3iBus41w")

# Print available models to console
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(m.name)