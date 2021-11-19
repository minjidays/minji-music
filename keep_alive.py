import tornado.web

import logging
logging.getLogger('tornado.access').disabled = True

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.write(""parabéns amigo, você é um amigo!")

def keep_alive():
  app = tornado.web.Application([(r"/", MainHandler)])
  app.listen(8080)
