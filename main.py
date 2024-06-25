from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello, this is the FastAPI application"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}    
