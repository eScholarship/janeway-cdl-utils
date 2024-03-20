from django.core.management.base import BaseCommand, CommandError

import requests, json

from journal.models import Journal
from review.models import ReviewAssignment, ReviewForm, ReviewAssignmentAnswer
from django.contrib.contenttypes.models import ContentType
from utils.models import LogEntry
from submission.models import Article

from bs4 import BeautifulSoup

class Command(BaseCommand):
    """Backfills review assignment comments to a given journal"""
    help = "Backfills review assignment comments to a given journal"

    def add_arguments(self, parser):
        parser.add_argument(
            "journal_code", help="`code` of the journal to add arks", type=str
        )
        parser.add_argument(
            "ojs_server", help="which server to read from dev/stg/prd", type=str
        )

    # Get the article OJS id from the import log
    def get_ojs_id(self, article):
        ctype = ContentType.objects.get(app_label='submission', model='article')
        desc = f'Article {article.pk} imported by Journal Transporter.'
        e = LogEntry.objects.filter(content_type=ctype, object_id=article.pk, description__startswith=desc)
        if e.count() == 1:
            d = json.loads(e[0].description.partition("Import metadata:")[2])
            for i in d['external_identifiers']:
                if i['name'] == "source_id":
                    return i['value']

        return None

    def strip_html(self, value):
        soup = BeautifulSoup(value, 'html.parser')
        return soup.get_text(separator="\n")

    def get_url_json(url):
        response = requests.get(url)
        if response.status_code != 200:
            raise CommandError(f"Get {url} failed with code {response.status_code}")
        return json.loads(response.text)

    # Extract id list from the OJS API
    def get_ids(self, url):
        data = self.get_url_json(url)
        return [x["source_record_key"].split(":")[-1] for x in data]

    def handle(self, *args, **options):
        ojs_code = options.get("journal_code")
        jcode = ojs_code[:24]
        server = options.get("ojs_server")

        ojs_url = f"https://pub-submit2-{server}.escholarship.org/ojs/index.php/pages/jt/api/journals"

        if not Journal.objects.filter(code=jcode).exists():
            raise CommandError(f'Journal does not exist {jcode}')

        journal = Journal.objects.get(code=jcode)
        # get default form that is created for each journal
        default_form = ReviewForm.objects.get(journal=journal, name="Default Form")
        # There is only one element to connect responses to
        form_element = default_form.elements.all()[0]

        # First, set the form to the default form for all Review Assignments
        # that don't currently have one
        x = ReviewAssignment.objects.filter(article__journal=journal, form=None)
        for r in x:
            r.form = default_form
            r.save()

        # Look for review comments for each article in this journal
        for article in Article.objects.filter(journal=journal):
            ojs_id = self.get_ojs_id(article)
            round_url = f"{ojs_url}/{ojs_code}/articles/{ojs_id}/rounds"
            try:
                rounds = self.get_ids(round_url)
                for r in rounds:
                    assignments_url = f"{round_url}/{r}/assignments"
                    assignments = self.get_ids(assignments_url)
                    for a_id in assignments:
                        assignment = self.get_url_json(f"{assignments_url}/{a_id}")
                        # only try and find the matching review assignment if there are
                        # comments to place
                        if len(assignment["comments"]) > 0:
                            # some assignments have the exact same date requested but I haven't found any that also have the exact date completed
                            ra = ReviewAssignment.objects.filter(date_requested=assignment["date_assigned"], date_complete=assignment["date_completed"])
                            # if the above doesn't produce a unique assignment report an error
                            if ra.count() > 1:
                                print(f"ERROR: found multiple assignments {ra}")
                                print(assignment)
                            elif ra.count() < 1:
                                print("ERROR: didn't find assignment")
                                print(assignment)
                            else:
                                r = ra[0]
                                for c in assignment["comments"]:
                                    # the first item that is not visible to the author goes into comments_for_editor
                                    # else they go in a form answer marked as author_can_see=False
                                    if not c["visible_to_author"] and (not r.comments_for_editor or len(r.comments_for_editor) == 0):
                                        r.comments_for_editor = self.strip_html(c["comments"])
                                    else:
                                        answer = ReviewAssignmentAnswer.objects.create(assignment=r,
                                                                                       original_element=form_element,
                                                                                       author_can_see=c["visible_to_author"],
                                                                                       answer=self.strip_html(c["comments"]))
                                        form_element.snapshot(answer)

                                r.save()
            except requests.exceptions.RequestException as e:
                raise CommandError(f'An error occurred: {e}')