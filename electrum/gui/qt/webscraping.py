from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings


class SitePreview(QWebEngineView):

    loaded_action = None

    def load_site(self, url):
        self.load(QUrl(url))
        self.page().settings().setAttribute(
            QWebEngineSettings.ShowScrollBars, False)
