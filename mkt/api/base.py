import functools
import json

from django.conf.urls.defaults import url
from django.db.models.sql import EmptyResultSet

import commonware.log
from rest_framework.decorators import api_view
from rest_framework.exceptions import ParseError
from rest_framework.mixins import ListModelMixin
from rest_framework.routers import Route, SimpleRouter
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

log = commonware.log.getLogger('z.api')


def list_url(name, **kw):
    kw['resource_name'] = name
    return ('api_dispatch_list', kw)


def get_url(name, pk, **kw):
    kw.update({'resource_name': name, 'pk': pk})
    return ('api_dispatch_detail', kw)

def _collect_form_errors(forms):
    errors = {}
    if not isinstance(forms, list):
        forms = [forms]
    for f in forms:
        # If we've got form objects, get the error object off it.
        # Otherwise assume we've just been passed a form object.
        form_errors = getattr(f, 'errors', f)
        if isinstance(form_errors, list):  # Cope with formsets.
            for e in form_errors:
                errors.update(e)
            continue
        errors.update(dict(form_errors.items()))
    return errors


def form_errors(forms):
    errors = _collect_form_errors(forms)
    raise ParseError(errors)

def check_potatocaptcha(data):
        if data.get('tuber', False):
            return Response(json.dumps({'tuber': 'Invalid value'}), 400)
        if data.get('sprout', None) != 'potato':
            return Response(json.dumps({'sprout': 'Invalid value'}), 400)


class AppRouter(SimpleRouter):
    routes = [
        # List route.
        Route(
            url=r'^{lookup}/{prefix}{trailing_slash}$',
            mapping={
                'get': 'list',
                'post': 'create'
            },
            name='{basename}-list',
            initkwargs={'suffix': 'List'}
        ),
        # Detail route.
        Route(
            url=r'^{lookup}/{prefix}{trailing_slash}$',
            mapping={
                'get': 'retrieve',
                'put': 'update',
                'post': 'detail_post',
                'patch': 'partial_update',
                'delete': 'destroy'
            },
            name='{basename}-detail',
            initkwargs={'suffix': 'Instance'}
        )
    ]


class SlugRouter(SimpleRouter):

    def get_urls(self):
        """
        Use the registered viewsets to generate a list of URL patterns.

        We can't use the superclass' implementation of get_urls since
        we want slug and pk urls for some resources, and it assumes
        one url per resource.
        """
        ret = []

        for prefix, viewset, basename in self.registry:
            routes = self.get_routes(viewset)

            for route in routes:
                # Only actions which actually exist on the viewset will be
                # bound.
                mapping = self.get_method_map(viewset, route.mapping)
                if not mapping:
                    continue

                # Build the url pattern
                if route.name.endswith('detail'):
                    slug_field = getattr(viewset, 'slug_lookup', None)
                    ret.append(self.create_url(prefix, viewset, basename,
                                               route, mapping, '(?P<pk>\d+)'))
                    if slug_field:
                        ret.append(self.create_url(
                            prefix, viewset, basename, route, mapping,
                            '(?P<%s>[^/<>"\']+)' % (slug_field,)))

                else:
                    ret.append(self.create_url(prefix, viewset, basename,
                                               route, mapping))
        return ret

    def create_url(self, prefix, viewset, basename, route, mapping, lookup=''):
        regex = route.url.format(prefix=prefix, lookup=lookup,
                                 trailing_slash=self.trailing_slash)
        view = viewset.as_view(mapping, **route.initkwargs)
        name = route.name.format(basename=basename)
        return url(regex, view, name=name)


class MarketplaceView(object):
    """
    Base view for DRF views.

    It includes:
    - An implementation of handle_exception() that goes with our custom
      exception handler. It stores the request and originating class in the
      exception before it's handed over the the handler, so that the handler
      can in turn properly propagate the got_request_exception signal if
      necessary.

    - A implementation of paginate_queryset() that goes with our custom
      pagination handler. It does tastypie-like offset pagination instead of
      the default page mechanism.
    """
    def handle_exception(self, exc):
        exc._request = self.request._request
        exc._klass = self.__class__
        return super(MarketplaceView, self).handle_exception(exc)

    def paginate_queryset(self, queryset, page_size=None):
        page_query_param = self.request.QUERY_PARAMS.get(self.page_kwarg)
        offset_query_param = self.request.QUERY_PARAMS.get('offset')

        # If 'offset' (tastypie-style pagination) parameter is present and
        # 'page' isn't, use offset it to find which page to use.
        if page_query_param is None and offset_query_param is not None:
            page_number = int(offset_query_param) / self.get_paginate_by() + 1
            self.kwargs[self.page_kwarg] = page_number
        return super(MarketplaceView, self).paginate_queryset(queryset,
            page_size=page_size)


class CORSMixin(object):
    """
    Mixin to enable CORS for DRF API.
    """
    def finalize_response(self, request, response, *args, **kwargs):
        if not hasattr(request._request, 'CORS'):
            request._request.CORS = self.cors_allowed_methods
        return super(CORSMixin, self).finalize_response(
            request, response, *args, **kwargs)


def cors_api_view(methods):
    def decorator(f):
        @api_view(methods)
        @functools.wraps(f)
        def wrapped(request):
            request._request.CORS = methods
            return f(request)
        return wrapped
    return decorator


class SlugOrIdMixin(object):
    """
    Because the `SlugRouter` is overkill. If the name of your
    `slug` is called something else, override `self.slug_field`.
    """

    def get_object(self, queryset=None):
        pk = self.kwargs.get('pk')
        if pk and not pk.isdigit():
            # If the `pk` contains anything other than a digit, it's a `slug`.
            self.kwargs.update(pk=None, slug=self.kwargs['pk'])
        return super(SlugOrIdMixin, self).get_object(queryset=queryset)


class SilentListModelMixin(ListModelMixin):
    """
    DRF's ListModelMixin that returns a 204_NO_CONTENT rather than flipping a
    500 or 404.
    """

    def list(self, *args, **kwargs):
        try:
            res = super(SilentListModelMixin, self).list(*args, **kwargs)
        except EmptyResultSet:
            return Response([])
        if res.status_code == 404:
            return Response([])
        return res
