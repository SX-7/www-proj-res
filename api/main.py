import os
from flask import Flask, jsonify, request
from google.cloud import language_v1, datastore, translate
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


@app.route("/api/token")
def get_token():
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "ApiTokens"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return [{"api_token": entity["api_token"]} for entity in data]


@app.route("/api/tracked_tags")
def get_taglist():
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "Tags"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return [
        {
            "entry_id": entity.key.id,
            "tag_name": entity["tag_name"],
            "start_time": entity["start_time"],
            "end_time": entity["end_time"],
            "processed_posts": entity["processed_posts"],
        }
        for entity in data
    ]


@app.route("/api/update")
def update_sentiment_data():
    # step 1. get basic data
    api_token = get_token()[0]["api_token"]
    taglist = get_taglist()
    # step 2. check every tag
    for tag_info in taglist:
        # if there's over 24 hours since a last update
        diff = datetime.datetime.now(tz=datetime.timezone.utc) - tag_info["start_time"]
        if diff.days >= 1:
            # get posts for that day
            post_list = get_wykop_posts(
                api_token,
                tag_info["tag_name"],
                tag_info["start_time"],
                tag_info["end_time"],
            )
            
                # filter out short posts
            filtered = [post for post in post_list if len(post)>200]
            if len(filtered) is not 0:
                datastore_client = datastore.Client()
                # put them into google translate
                client = translate.TranslationServiceClient()
                kind = "ProjectId"
                query = datastore_client.query(kind=kind)
                data = list(query.fetch())
                parent = data[0]["project_id"]
                # Translate text from English to French
                # Detail on supported types can be found here:
                # https://cloud.google.com/translate/docs/supported-formats
                response = client.translate_text(
                    request={
                        "parent": parent,
                        "contents": filtered,
                        "mime_type": "text/plain",  # mime types: text/plain, text/html
                        "source_language_code": "pl",
                        "target_language_code": "en",
                    }
                )
                translations = [translation.translated_text for translation in response.translations]
                # put the translated text into AI - optimizations pending cuz it's like way too expensive as is
                # client = language_v1.LanguageServiceClient()
                # type_ = language_v1.Document.Type.PLAIN_TEXT
                # document = {"type_": type_, "content": content}
                # response = client.analyze_sentiment(request={"document": document})
                # sentiment = response.document_sentiment
                # put the sentiment data back into db
            
            # update the time period
    return translations


def get_wykop_posts(
    api_token: int,
    tag_name: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
):
    wykop_data = json.loads(
        requests.get(
            f"https://wykop.pl/api/v3/search/entries",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {api_token}",
            },
            params={
                "query": f"#{tag_name}",
                "sort": "newest",
                "votes": "100",
                "date_from": f'{start_time.strftime("%Y-%m-%d %H:%M:%S")}',
                "date_to": f'{end_time.strftime("%Y-%m-%d %H:%M:%S")}',
                "page": "1",
                "limit": "25",
            },
        ).content.decode("utf-8")
    )
    results = list()
    for v in wykop_data["data"]:
        results.append(v["content"])
    if wykop_data["pagination"]["total"] > 25:
        page = 2
        while wykop_data["pagination"]["total"] > (page - 1) * 25:
            wykop_data = json.loads(
                requests.get(
                    f"https://wykop.pl/api/v3/search/entries",
                    headers={
                        "accept": "application/json",
                        "authorization": f"Bearer {api_token}",
                    },
                    params={
                        "query": f"#{tag_name}",
                        "sort": "newest",
                        "votes": "100",
                        "date_from": f'{start_time.strftime("%Y-%m-%d %H:%M:%S")}',
                        "date_to": f'{end_time.strftime("%Y-%m-%d %H:%M:%S")}',
                        "page": f"{page}",
                        "limit": "25",
                    },
                ).content.decode("utf-8")
            )
            for v in wykop_data["data"]:
                results.append(v["content"])
            page += 1
    if len(results) is not 0:
        # format posts       
        untagged = [re.sub(r"\#\S+\b\s?",'',post) for post in results]
        newlined = [re.sub(r"\n",' ',post) for post in untagged]
        carriaged = [re.sub(r"\r",'',post) for post in newlined]
        unmarked = [re.sub(r"[\[\*\]]",'',post) for post in carriaged]
        unlinked = [re.sub(r"https?://\S+(?=[\s)])",'',post) for post in unmarked]
        cleaned = [re.sub(r"\(\)",'',post) for post in unlinked]
    return cleaned

### --- things below to be done later ---


#@app.route("/api/ai")
def get_sentiments():
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "Sentiment"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return [
        {
            "key_id": entity.id,
            "content": entity["content"],
            "score": entity["score"],
            "magnitude": entity["magnitude"],
        }
        for entity in data
    ]


#@app.route("/api/ai", methods=["POST"])
def sample_analyze_sentiment():
    # initialize language analyzer connection
    
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
