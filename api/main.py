import os
from flask import Flask, jsonify, request
from google.cloud import language_v1, datastore
import requests
from bs4 import BeautifulSoup
import re
import requests
import base64
import json
import datetime

app = Flask(__name__)

# question with a $0 prize: why aren't we using PUT?
# answer: cron.yaml functions by sending GET requests, *only*
# addendum: modify to disallow external connections after testing is done
# https://cloud.google.com/appengine/docs/flexible/scheduling-jobs-with-cron-yaml#securing_urls_for_cron
@app.route("/api/token/refresh")
def refresh_token():
    # here we basically wanna implement the entirety of token scrapper
    # GET request for a sample page, to get the vendor info
    page = requests.get("https://wykop.pl/faq")
    soup = BeautifulSoup(page.content, "html.parser")
    vendor_token = soup.find("meta", attrs={"name": "build.vendor"})["content"]
    # use the token to retrieve the API keys
    constructed_url = f"https://wykop.pl/static/js/{vendor_token}.js"
    api_file = requests.get(constructed_url)
    # what it does: gets the container for "apiLocation", from which we extract apiClientId and apiClientSecret
    # TODO: more redundacy, rn we're using hardcoded 2 and 3 elements, which works, but can break
    matches = (
        re.search("\{([^{}]*)apiLocation([^{}]*)\}", str(api_file.content))
        .group()
        .strip("\{\}")
        .split(",")
    )
    api_client_id = base64.b64decode(matches[1].split(":")[1].strip('""')).decode(
        "utf-8"
    )
    api_client_secret = base64.b64decode(matches[2].split(":")[1].strip('""')).decode(
        "utf-8"
    )
    auth_data = requests.post(
        "https://wykop.pl/api/v3/auth",
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/114.0",
            "Accept": "application/json",
            "Content-Type": "application/json;charset=utf-8",
        },
        json={
            "data": {
                "key": api_client_id,
                "secret": api_client_secret,
            }
        },
    )
    # rn we don't error check, but this is our token if all goes well :)
    api_token = json.loads(auth_data.content.decode("utf-8"))["data"]["token"]
    # Connects to db, prepares the new entity and sends it. Default overwrite ensures only one token will exist
    datastore_client = datastore.Client()
    kind = "ApiTokens"
    record_name = "token"
    entity_key = datastore_client.key(kind, record_name)
    entity = datastore.Entity(key=entity_key)
    entity["api_token"] = api_token
    datastore_client.put(entity=entity)
    return "", 204

@app.route("/api/token/get")
def get_token():
# iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "ApiTokens"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return jsonify(
        [
            {
                "api_token": entity["api_token"]
            }
            for entity in data
        ]
    )

@app.route("/api/tracked_tags")
def get_taglist(pythonic=False):
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "Tags"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return_data = [
        {
            "tag_name": entity["tag_name"],
            "start_time": entity["start_time"],
            "end_time": entity["end_time"],
            "processed_posts":entity["processed_posts"]
        }
        for entity in data
    ]
    if pythonic: # for in-program use
        return return_data
    else:
        return jsonify(return_data)


@app.route("/api/mock")
def get_some_wykop_data():
    api_token = get_token().json[0]['api_token']
    tag_info = get_taglist(pythonic=True)
    for tag_data in tag_info:
        page_offset = (tag_data["processed_posts"]/25)+1
        wykop_data = requests.get(
            f'https://wykop.pl/api/v3/tags/{tag_data["tag_name"]}/stream',
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {api_token}",
            },
            
            params={
                'query': '#polska',
                'sort': 'newest',
                'votes': '100',
                'date_from': f"{tag_data['start_time'].strftime('%Y-%m-%d %H:%M:%S')}",
                'date_to': f"{tag_data['end_time'].strftime('%Y-%m-%d %H:%M:%S')}",
                'page': f'{page_offset}',
                'limit': '25',
            }
        )
    return jsonify(wykop_data.content.decode("utf-8"))

@app.route("/api/ai")
def get_sentiments():
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "Sentiment"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return jsonify(
        [
            {
                "key_id": entity.id,
                "content": entity["content"],
                "score": entity["score"],
                "magnitude": entity["magnitude"],
            }
            for entity in data
        ]
    )


@app.route("/api/ai", methods=["POST"])
def sample_analyze_sentiment():
    # initialize language analyzer connection
    client = language_v1.LanguageServiceClient()
    # get request info
    content = request.get_json()
    # check for compliance with arbitrary API rules
    if "content" in content:
        content = content["content"]
    else:
        return "", 400
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
    return "", 204


@app.route("/api/ai", methods=["DELETE"])
def delete_sentiment_entry():
    record_id = request.get_json()
    if "record_id" in record_id:
        record_id = record_id["record_id"]
    else:
        return "", 400
    datastore_client = datastore.Client()
    kind = "Sentiment"
    entity_key = datastore_client.key(kind, record_id)
    datastore_client.delete(entity_key)
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
