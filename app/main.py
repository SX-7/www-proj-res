import os
from flask import Flask
import requests

app = Flask(__name__)


@app.route("/")
def hello():
    # https://cloud.google.com/appengine/docs/flexible/how-requests-are-routed?tab=python
    # add '?tag=rosja' or '?tag=polska' or even '?tag=rosja&?tag=polska' to narrow results, or leave blank to get all
    # filtered post total - how many posts over 100 upvotes were posted that day (note: not all were processed!)
    # post total - simply count of posts with this tag, that day
    # upvote total - how many upvotes in posts over 100 upvotes
    # weighted average - positive/negative perception, calculated with score as values, and confidence as weights
    # upvoted weighted average - upvotes are also added as weights, makes more upvoted posts more impactful
    r = requests.get("https://www-server-resume-1.ew.r.appspot.com/api/get")
    return r.json()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
