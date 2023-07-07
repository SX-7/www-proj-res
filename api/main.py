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
            (
                post_list,
                filtered_upvote_total,
                filtered_post_total,
                post_total,
            ) = get_wykop_posts(
                api_token,
                tag_info["tag_name"],
                tag_info["start_time"],
                tag_info["end_time"],
            )
            normal_weighted_average = 0
            upvoted_weighted_average = 0
            # filter out short posts
            filtered = [post for post in post_list if len(post["content"]) > 200]
            if len(filtered) is not 0:
                datastore_client = datastore.Client()
                # put them into google translate
                client = translate.TranslationServiceClient()
                kind = "ProjectId"
                query = datastore_client.query(kind=kind)
                data = list(query.fetch())
                parent = data[0]["project_id"]
                translations = list()
                for post in filtered:
                    response = client.translate_text(
                        request={
                            "parent": parent,
                            "contents": [post["content"]],
                            "mime_type": "text/plain",  # mime types: text/plain, text/html
                            "source_language_code": "pl",
                            "target_language_code": "en",
                        }
                    )
                    translations.append(
                        {
                            "content": response.translations[0].translated_text,
                            "votes": post["votes"],
                        }
                    )
                # put the translated text into AI
                client = language_v1.LanguageServiceClient()
                type_ = language_v1.Document.Type.PLAIN_TEXT
                analysis = list()
                for content in translations:
                    document = {"type_": type_, "content": content["content"]}
                    response = client.analyze_sentiment(request={"document": document})
                    sentiment = response.document_sentiment
                    analysis.append(
                        {
                            "content": content["content"],
                            "score": sentiment.score,
                            "magnitude": sentiment.magnitude,
                            "votes": content["votes"],
                        }
                    )

                normal_weighted_average = sum(
                    (
                        x * y
                        for x, y in zip(
                            (postx["score"] for postx in analysis),
                            (posty["magnitude"] for posty in analysis),
                        )
                    )
                ) / sum((postz["magnitude"] for postz in analysis))

                upvoted_weighted_average = sum(
                    (
                        x * y * z
                        for x, y, z in zip(
                            (postx["score"] for postx in analysis),
                            (posty["magnitude"] for posty in analysis),
                            (postz["votes"] for postz in analysis),
                        )
                    )
                ) / sum(
                    (
                        a * b
                        for a, b in zip(
                            (posta["magnitude"] for posta in analysis),
                            (postb["votes"] for postb in analysis),
                        )
                    )
                )

            # put the sentiment data back into db
            # general idea is to use seperate kind (TagInfo) to store well, tag info
            # and to use it to be able to put only data related to a tag into the DB
            # this is mostly to organize data, instead of putting everything in one table
            # The kind for the new entity
            kind = "Sentiment" + str(tag_info["tag_name"])
            # The Cloud Datastore key for the new entity
            entity_key = datastore_client.key(kind)

            # Prepares the new entity
            entity = datastore.Entity(key=entity_key)
            entity["upvote_total"] = filtered_upvote_total
            entity["post_total"] = post_total
            entity["filtered_post_total"] = filtered_post_total
            entity["weighted_average"] = normal_weighted_average
            entity["upvoted_weighted_average"] = upvoted_weighted_average
            entity["year"] = tag_info["start_time"].year
            entity["month"] = tag_info["start_time"].month
            entity["day"] = tag_info["start_time"].day
            # Saves the entity
            datastore_client.put(entity)

            # update the time period
            # setup and execute get
            kind = "Tags"
            target_key = datastore_client.key(kind,tag_info["entry_id"])
            curr_tag_info = datastore_client.get(target_key)
            curr_tag_info["start_time"]=tag_info["end_time"]
            curr_tag_info["end_time"]=tag_info["end_time"]+datetime.timedelta(1)
            curr_tag_info["processed_posts"]+=filtered_post_total
            # return query results to the user
            datastore_client.put(curr_tag_info)

    return "", 204


def get_wykop_posts(
    api_token: int,
    tag_name: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
):
    cleaned = []
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
        results.append({"content": v["content"], "votes": v["votes"]["up"]})
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
                results.append({"content": v["content"], "votes": v["votes"]["up"]})
            page += 1

    filtered_upvote_total = 0
    filtered_post_total = len(results)
    for post in results:
        filtered_upvote_total += post["votes"]

    if len(results) is not 0:
        # format posts
        untagged = [
            {
                "content": re.sub(r"\#\S+\b\s?", "", post["content"]),
                "votes": post["votes"],
            }
            for post in results
        ]
        newlined = [
            {"content": re.sub(r"\n", " ", post["content"]), "votes": post["votes"]}
            for post in untagged
        ]
        carriaged = [
            {"content": re.sub(r"\r", "", post["content"]), "votes": post["votes"]}
            for post in newlined
        ]
        unmarked = [
            {
                "content": re.sub(r"[\[\*\]]", "", post["content"]),
                "votes": post["votes"],
            }
            for post in carriaged
        ]
        unlinked = [
            {
                "content": re.sub(r"https?://\S+(?=[\s)])", "", post["content"]),
                "votes": post["votes"],
            }
            for post in unmarked
        ]
        cleaned = [
            {"content": re.sub(r"\(\)", "", post["content"]), "votes": post["votes"]}
            for post in unlinked
        ]
    
    post_total = json.loads(
        requests.get(
            f"https://wykop.pl/api/v3/search/entries",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {api_token}",
            },
            params={
                "query": f"#{tag_name}",
                "sort": "newest",
                "date_from": f'{start_time.strftime("%Y-%m-%d %H:%M:%S")}',
                "date_to": f'{end_time.strftime("%Y-%m-%d %H:%M:%S")}',
                "page": "1",
                "limit": "25",
            },
        ).content.decode("utf-8")
    )["pagination"]["total"]

    try:
        post_total = int(post_total)
    except:
        post_total=0

    return cleaned, filtered_upvote_total, filtered_post_total, post_total


### --- things below to be done later ---


# @app.route("/api/ai")
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


# @app.route("/api/ai", methods=["POST"])
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
