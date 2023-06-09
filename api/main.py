import os
from flask import Flask, request
from google.cloud import language_v1, datastore, translate
import requests
from bs4 import BeautifulSoup
import re
import requests
import base64
import json
import datetime
import time

app = Flask(__name__)


# question with a $0 prize: why aren't we using PUT?
# answer: cron.yaml functions by sending GET requests, *only*
@app.route("/api/token/refresh")
def refresh_token():
    # Check if it's a crontab job
    if "X-Appengine-Cron" not in request.headers:
        return "Unauthorized request", 401
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
    entity = datastore.Entity(key=entity_key,exclude_from_indexes=("api_token",))
    entity["api_token"] = api_token
    datastore_client.put(entity=entity)
    return "", 204


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
def get_small_taglist():
    datastore_client = datastore.Client()
    # setup and execute query
    kind = "Tags"
    query = datastore_client.query(kind=kind)
    data = list(query.fetch())
    # return query results to the user
    return [{"tag_name": entity["tag_name"]} for entity in data]


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
            "current_time": entity["current_time"],
            "processed_posts": entity["processed_posts"],
        }
        for entity in data
    ]


@app.route("/api/update")
def update_sentiment_data_manager():
    # Check if it's a crontab job
    if "X-Appengine-Cron" not in request.headers:
        return "Unauthorized request", 401
    # get the time 
    start_time = time.time()
    # batches jobs, generally ensures we won't go into overtime
    while time.time() - start_time < 15:
        update_sentiment_data()
    return "",204

def update_sentiment_data():
    # step 1. get basic data
    taglist = get_taglist()
    # step 2. check every tag, we want the one which hasn't been updated yet
    chosen_tag = {"current_time":datetime.datetime.now(tz=datetime.timezone.utc)}
    for tag_info in taglist:
        if tag_info["current_time"]<chosen_tag["current_time"]:
            chosen_tag=tag_info
    tag_info=chosen_tag
    
    # if there's over 24 hours since a last update
    diff = datetime.datetime.now(tz=datetime.timezone.utc) - tag_info["current_time"]
    if diff.days >= 1:
        # to allow re-doing data, or going even further back, 
        # without incurring charges for unnecesary API usage, we're gonna first check if there's already a db entry
        datastore_client = datastore.Client()
        kind = f"Sentiment_{tag_info['tag_name']}"
        query = datastore_client.query(kind=kind)
        query.add_filter("year","=",tag_info["current_time"].year)
        query.add_filter("month","=",tag_info["current_time"].month)
        query.add_filter("day","=",tag_info["current_time"].day)
        fits = list(query.fetch())
        if len(fits) == 0:
            api_token = get_token()[0]["api_token"]
            # that means there's no entries for this day
            # get posts for that day
            (
                post_list,
                filtered_upvote_total,
                post_total,
            ) = get_wykop_posts(
                api_token,
                tag_info["tag_name"],
                tag_info["current_time"],
                tag_info["current_time"]+datetime.timedelta(1),
            )
            normal_weighted_average = 0
            upvoted_weighted_average = 0
            # filter out short posts
            filtered = [post for post in post_list if len(post["content"]) > 200]
            filtered_post_total = len(filtered)
            
            if len(filtered) != 0:
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
                    try:
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
                    except:
                        analysis.append(
                            {
                                "content": "nil",
                                "score": 0,
                                "magnitude": 0,
                                "votes": 0,
                            }
                        )
                try:
                    normal_weighted_average = sum(
                        (
                            x * y
                            for x, y in zip(
                                (postx["score"] for postx in analysis),
                                (posty["magnitude"] for posty in analysis),
                            )
                        )
                    ) / sum((postz["magnitude"] for postz in analysis))
                except:
                    normal_weighted_average = 0
                try:
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
                except:
                    upvoted_weighted_average=0

            # put the sentiment data back into db
            # general idea is to use seperate kind (TagInfo) to store well, tag info
            # and to use it to be able to put only data related to a tag into the DB
            # this is mostly to organize data, instead of putting everything in one table
            # The kind for the new entity
            kind = "Sentiment_" + str(tag_info["tag_name"])
            # The Cloud Datastore key for the new entity
            entity_key = datastore_client.key(kind)

            # Prepares the new entity
            entity = datastore.Entity(key=entity_key,exclude_from_indexes=("upvote_total","post_total","filtered_post_total","weighted_average","upvoted_weighted_average"))
            entity["upvote_total"] = filtered_upvote_total
            entity["post_total"] = post_total
            entity["filtered_post_total"] = filtered_post_total
            entity["weighted_average"] = normal_weighted_average
            entity["upvoted_weighted_average"] = upvoted_weighted_average
            entity["year"] = tag_info["current_time"].year
            entity["month"] = tag_info["current_time"].month
            entity["day"] = tag_info["current_time"].day
            # Saves the entity
            datastore_client.put(entity)
        # regardless of an entry existing, we still need to update the timer
        # update the time period
        # setup and execute get
        kind = "Tags"
        target_key = datastore_client.key(kind, tag_info["entry_id"])
        curr_tag_info = datastore_client.get(target_key)
        curr_tag_info["current_time"] = tag_info["current_time"] + datetime.timedelta(1)
        try:
            curr_tag_info["processed_posts"] += len(filtered)
        except:
            # we just don't add nothin', it occurs when skipping the block above
            pass
        # return query results to the db
        datastore_client.put(curr_tag_info)


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
    for post in results:
        filtered_upvote_total += post["votes"]

    if len(results) != 0:
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

    post_total = wykop_data["pagination"]["total"]

    try:
        post_total = int(post_total)
    except:
        post_total = 0

    return cleaned, filtered_upvote_total, post_total


@app.route("/api/get")
def get_sentiments():
    # basically, get a http GET request, use info provided by the request to query db and return data
    tags = request.args.getlist("tag")
    # if tags = empty => assume querying for everything
    if len(tags) == 0:
        tracked_list = get_taglist()
        tags = [tag["tag_name"] for tag in tracked_list]
    # iniialize db connection
    datastore_client = datastore.Client()
    # setup and execute query
    reval = {}
    for tag in tags:
        kind = "Sentiment_" + str(tag)
        query = datastore_client.query(kind=kind)
        data = query.fetch()
        # return query results to the user
        tag_dict = {}
        for entry in data:
            day = str(entry["day"])
            if len(day) == 1:
                day = "0" + day
            month = str(entry["month"])
            if len(month) == 1:
                month = "0" + month

            tag_dict[f"{str(entry['year'])}-{month}-{day}"] = {
                "upvote_total": entry["upvote_total"],
                "post_total": entry["post_total"],
                "filtered_post_total": entry["filtered_post_total"],
                "weighted_average": entry["weighted_average"],
                "upvoted_weighted_average": entry["upvoted_weighted_average"],
            }
        reval[tag] = tag_dict
    return reval


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
