import os
from flask import Flask, jsonify, request
from google.cloud import language_v1, datastore
app = Flask(__name__)

@app.route('/api')
def get_sentiments():
    datastore_client = datastore.Client()
    kind = "Sentiment"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    print("Returned query count is: " + str(len(data)))
    return jsonify([{
         "key_id":entity.id(),
         "content":entity["content"],
         "score":entity["score"],
         "magnitude":entity["magnitude"],
    } for entity in data])

@app.route('/api', methods=["POST"])
def sample_analyze_sentiment():
    # analyze content
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

    # Instantiates a client
    datastore_client = datastore.Client()

    # The kind for the new entity
    kind = "Sentiment"
    # The Cloud Datastore key for the new entity
    entity_key = datastore_client.key(kind)

    # Prepares the new entity
    entity = datastore.Entity(key=entity_key)
    entity["content"] = content
    entity["score"] = sentiment.score
    entity["magnitude"] = sentiment.magnitude

    # Saves the entity
    datastore_client.put(entity)
    return '', 204

if __name__ == '__main__':
	app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))