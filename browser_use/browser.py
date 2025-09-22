from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

class Browser:
    def __init__(self, headless=False):
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=options)

    def close(self):
        try:
            self.driver.quit()
        except:
            pass
