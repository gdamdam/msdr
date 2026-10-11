"""Domain mixins that make up msdr.api.service.AppService.

Each module holds one resource family's methods (the same boundaries
msdr/api/app.py groups its routes by). The mixins carry no state of
their own: every attribute they touch is initialised in
AppService.__init__.
"""
