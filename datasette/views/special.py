import json
from datasette.permissions import PERMISSIONS
from datasette.utils.asgi import Response, Forbidden
from datasette.utils import actor_matches_allow, add_cors_headers
from datasette.permissions import PERMISSIONS
from .base import BaseView
import secrets
import time


class JsonDataView(BaseView):
    name = "json_data"

    def __init__(self, datasette, filename, data_callback, needs_request=False):
        self.ds = datasette
        self.filename = filename
        self.data_callback = data_callback
        self.needs_request = needs_request

    async def get(self, request):
        as_format = request.url_vars["format"]
        await self.ds.ensure_permissions(request.actor, ["view-instance"])
        if self.needs_request:
            data = self.data_callback(request)
        else:
            data = self.data_callback()
        if as_format:
            headers = {}
            if self.ds.cors:
                add_cors_headers(headers)
            return Response(
                json.dumps(data),
                content_type="application/json; charset=utf-8",
                headers=headers,
            )

        else:
            return await self.render(
                ["show_json.html"],
                request=request,
                context={
                    "filename": self.filename,
                    "data_json": json.dumps(data, indent=4),
                },
            )


class PatternPortfolioView(BaseView):
    name = "patterns"
    has_json_alternate = False

    async def get(self, request):
        await self.ds.ensure_permissions(request.actor, ["view-instance"])
        return await self.render(["patterns.html"], request=request)


class AuthTokenView(BaseView):
    name = "auth_token"
    has_json_alternate = False

    async def get(self, request):
        token = request.args.get("token") or ""
        if not self.ds._root_token:
            raise Forbidden("Root token has already been used")
        if secrets.compare_digest(token, self.ds._root_token):
            self.ds._root_token = None
            response = Response.redirect(self.ds.urls.instance())
            response.set_cookie(
                "ds_actor", self.ds.sign({"a": {"id": "root"}}, "actor")
            )
            return response
        else:
            raise Forbidden("Invalid token")


class LogoutView(BaseView):
    name = "logout"
    has_json_alternate = False

    async def get(self, request):
        if not request.actor:
            return Response.redirect(self.ds.urls.instance())
        return await self.render(
            ["logout.html"],
            request,
            {"actor": request.actor},
        )

    async def post(self, request):
        response = Response.redirect(self.ds.urls.instance())
        response.set_cookie("ds_actor", "", expires=0, max_age=0)
        self.ds.add_message(request, "You are now logged out", self.ds.WARNING)
        return response


class PermissionsDebugView(BaseView):
    name = "permissions_debug"
    has_json_alternate = False

    async def get(self, request):
        await self.ds.ensure_permissions(request.actor, ["view-instance"])
        if not await self.ds.permission_allowed(request.actor, "permissions-debug"):
            raise Forbidden("Permission denied")
        return await self.render(
            ["permissions_debug.html"],
            request,
            # list() avoids error if check is performed during template render:
            {
                "permission_checks": list(reversed(self.ds._permission_checks)),
                "permissions": PERMISSIONS,
            },
        )

    async def post(self, request):
        await self.ds.ensure_permissions(request.actor, ["view-instance"])
        if not await self.ds.permission_allowed(request.actor, "permissions-debug"):
            raise Forbidden("Permission denied")
        vars = await request.post_vars()
        actor = json.loads(vars["actor"])
        permission = vars["permission"]
        resource_1 = vars["resource_1"]
        resource_2 = vars["resource_2"]
        resource = []
        if resource_1:
            resource.append(resource_1)
        if resource_2:
            resource.append(resource_2)
        resource = tuple(resource)
        result = await self.ds.permission_allowed(
            actor, permission, resource, default="USE_DEFAULT"
        )
        return Response.json(
            {
                "actor": actor,
                "permission": permission,
                "resource": resource,
                "result": result,
            }
        )


class AllowDebugView(BaseView):
    name = "allow_debug"
    has_json_alternate = False

    async def get(self, request):
        errors = []
        actor_input = request.args.get("actor") or '{"id": "root"}'
        try:
            actor = json.loads(actor_input)
            actor_input = json.dumps(actor, indent=4)
        except json.decoder.JSONDecodeError as ex:
            errors.append(f"Actor JSON error: {ex}")
        allow_input = request.args.get("allow") or '{"id": "*"}'
        try:
            allow = json.loads(allow_input)
            allow_input = json.dumps(allow, indent=4)
        except json.decoder.JSONDecodeError as ex:
            errors.append(f"Allow JSON error: {ex}")

        result = None
        if not errors:
            result = str(actor_matches_allow(actor, allow))

        return await self.render(
            ["allow_debug.html"],
            request,
            {
                "result": result,
                "error": "\n\n".join(errors) if errors else "",
                "actor_input": actor_input,
                "allow_input": allow_input,
            },
        )


class MessagesDebugView(BaseView):
    name = "messages_debug"
    has_json_alternate = False

    async def get(self, request):
        await self.ds.ensure_permissions(request.actor, ["view-instance"])
        return await self.render(["messages_debug.html"], request)

    async def post(self, request):
        await self.ds.ensure_permissions(request.actor, ["view-instance"])
        post = await request.post_vars()
        message = post.get("message", "")
        message_type = post.get("message_type") or "INFO"
        assert message_type in ("INFO", "WARNING", "ERROR", "all")
        datasette = self.ds
        if message_type == "all":
            datasette.add_message(request, message, datasette.INFO)
            datasette.add_message(request, message, datasette.WARNING)
            datasette.add_message(request, message, datasette.ERROR)
        else:
            datasette.add_message(request, message, getattr(datasette, message_type))
        return Response.redirect(self.ds.urls.instance())


class CreateTokenView(BaseView):
    name = "create_token"
    has_json_alternate = False

    def check_permission(self, request):
        if not self.ds.setting("allow_signed_tokens"):
            raise Forbidden("Signed tokens are not enabled for this Datasette instance")
        if not request.actor:
            raise Forbidden("You must be logged in to create a token")
        if not request.actor.get("id"):
            raise Forbidden(
                "You must be logged in as an actor with an ID to create a token"
            )
        if request.actor.get("token"):
            raise Forbidden(
                "Token authentication cannot be used to create additional tokens"
            )

    async def get(self, request):
        self.check_permission(request)
        return await self.render(
            ["create_token.html"],
            request,
            {"actor": request.actor},
        )

    async def post(self, request):
        self.check_permission(request)
        post = await request.post_vars()
        errors = []
        duration = None
        if post.get("expire_type"):
            duration_string = post.get("expire_duration")
            if (
                not duration_string
                or not duration_string.isdigit()
                or not int(duration_string) > 0
            ):
                errors.append("Invalid expire duration")
            else:
                unit = post["expire_type"]
                if unit == "minutes":
                    duration = int(duration_string) * 60
                elif unit == "hours":
                    duration = int(duration_string) * 60 * 60
                elif unit == "days":
                    duration = int(duration_string) * 60 * 60 * 24
                else:
                    errors.append("Invalid expire duration unit")
        token_bits = None
        token = None
        if not errors:
            token_bits = {
                "a": request.actor["id"],
                "t": int(time.time()),
            }
            if duration:
                token_bits["d"] = duration
            token = "dstok_{}".format(self.ds.sign(token_bits, "token"))
        return await self.render(
            ["create_token.html"],
            request,
            {
                "actor": request.actor,
                "errors": errors,
                "token": token,
                "token_bits": token_bits,
            },
        )


class ApiExplorerView(BaseView):
    name = "api_explorer"
    has_json_alternate = False

    async def get(self, request):
        return await self.render(["api_explorer.html"], request)
