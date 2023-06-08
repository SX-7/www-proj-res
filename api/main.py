import os
from flask import Flask, jsonify, request
from google.cloud import language_v1, datastore
app = Flask(__name__)

@app.route('/api')
def get_sentiments():
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "Sentiment"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return jsonify([{
         "key_id":entity.id,
         "content":entity["content"],
         "score":entity["score"],
         "magnitude":entity["magnitude"],
    } for entity in data])

@app.route('/api', methods=["POST"])
def sample_analyze_sentiment():
    # initialize language analyzer connection
    client = language_v1.LanguageServiceClient()
    # get request info
    content = request.get_json()
    # check for compliance with arbitrary API rules
    if "content" in content:
        content = content["content"]
    else:
        return '', 400
    # JIC
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    # analyze
    type_ = language_v1.Document.Type.PLAIN_TEXT
    document = {"type_": type_, "content": content}
    response = client.analyze_sentiment(request={"document": document})
    sentiment = response.document_sentiment

    # Instantiates a db client
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

@app.route('/api', methods=['DELETE'])
def delete_sentiment_entry():
    record_id = request.get_json
    if "record_id" in record_id:
        record_id = record_id["record_id"]
    else:
        return '', 400
    datastore_client = datastore.Client()
    kind = "Sentiment"
    entity_key = datastore_client.key(kind,record_id)
    datastore_client.delete(entity_key)
    return '', 204

if __name__ == '__main__':
	app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))