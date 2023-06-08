import os
from flask import Flask, jsonify, request
from google.cloud import language_v1
app = Flask(__name__)

tests = [
      {
            "content": "test"
      }
]

@app.route('/api')
def hello():
	return jsonify(tests)

@app.route('/api', methods=["POST"])
def sample_analyze_sentiment():

    client = language_v1.LanguageServiceClient()

    content = request.get_json()

    if "content" in content:
        content = content["content"]
    else:
        return '', 400
    
    if isinstance(content, bytes):
        content = content.decode("utf-8")

    type_ = language_v1.Document.Type.PLAIN_TEXT
    document = {"type_": type_, "content": content}

    response = client.analyze_sentiment(request={"document": document})
    sentiment = response.document_sentiment
    
    tests.append({"content":content,"score":sentiment.score,"magnitude":sentiment.magnitude})
    return '', 204

if __name__ == '__main__':
	app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))