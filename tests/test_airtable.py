import unittest

from app.sources.airtable import extract_topic_urls, topic_details


class AirtableParserTests(unittest.TestCase):
    def test_extracts_unique_topics_from_listing(self):
        page = '''<a href="https://community.airtable.com/jobs-board-16/need-make-api-48303">one</a>
        <a href="https://community.airtable.com/jobs-board-16/need-make-api-48303?postid=1">reply</a>'''
        self.assertEqual(extract_topic_urls(page, "https://community.airtable.com/jobs-board-16"), [
            ("https://community.airtable.com/jobs-board-16/need-make-api-48303", "48303")])

    def test_extracts_public_topic_data(self):
        page = '''<title>Need Make &amp; API help | Airtable Community</title>
        <meta name="description" content="Looking for freelance automation support.">
        <time dateTime="2026-08-22">today</time>'''
        title, body, published_at = topic_details(page)
        self.assertEqual((title, body, published_at.isoformat()),
            ("Need Make & API help", "Looking for freelance automation support.", "2026-08-22T00:00:00+00:00"))
