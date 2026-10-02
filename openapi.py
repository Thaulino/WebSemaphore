"""The OpenAPI 3.1 description of the REST API, served at /api/openapi.json."""

ERROR = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
}

RESOURCE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string", "examples": ["lab-pi-03"]},
        "description": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "capacity": {"type": "integer", "description": "Total slots."},
        "in_use": {"type": "integer"},
        "free": {"type": "integer"},
    },
}

LOCK = {
    "type": "object",
    "properties": {
        "lock_id": {"type": "integer"},
        "resource_id": {"type": "integer"},
        "resource": {"type": "string"},
        "note": {"type": "string"},
        "acquired_at": {"type": "integer", "description": "Unix timestamp."},
        "user": {"type": "string"},
    },
}

UNAUTHORIZED = {
    "description": "Missing, malformed or revoked token.",
    "content": {"application/json": {"schema": ERROR}},
}


def spec():
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Semaphore API",
            "version": "1.0.0",
            "description": (
                "Lock shared machines and test devices from scripts and CI.\n\n"
                "Create a token under Settings in the web UI, then send it as "
                "`Authorization: Bearer sem_...`.\n\n"
                "A lock can target one specific resource by id or name, or any "
                "free resource carrying a given label."
            ),
        },
        "servers": [{"url": "/", "description": "This server"}],
        "security": [{"bearerAuth": []}],
        "tags": [
            {"name": "resources", "description": "Browse shared resources."},
            {"name": "locks", "description": "Take and release slots."},
        ],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "An API token created in the web UI.",
                }
            },
            "schemas": {"Resource": RESOURCE, "Lock": LOCK, "Error": ERROR},
        },
        "paths": {
            "/api/me": {
                "get": {
                    "summary": "Identify the token holder",
                    "operationId": "getMe",
                    "responses": {
                        "200": {
                            "description": "The account the token belongs to.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "username": {"type": "string"},
                                            "is_admin": {"type": "boolean"},
                                        },
                                    }
                                }
                            },
                        },
                        "401": UNAUTHORIZED,
                    },
                }
            },
            "/api/v1/resources": {
                "get": {
                    "tags": ["resources"],
                    "summary": "List resources",
                    "operationId": "listResources",
                    "parameters": [
                        {
                            "name": "label",
                            "in": "query",
                            "description": "Only resources carrying this label.",
                            "schema": {"type": "string"},
                            "example": "rack-b",
                        },
                        {
                            "name": "free",
                            "in": "query",
                            "description": "Only resources with a slot available.",
                            "schema": {"type": "boolean"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Matching resources with live occupancy.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "resources": {
                                                "type": "array",
                                                "items": RESOURCE,
                                            }
                                        },
                                    }
                                }
                            },
                        },
                        "401": UNAUTHORIZED,
                    },
                }
            },
            "/api/v1/resources/{resource}": {
                "get": {
                    "tags": ["resources"],
                    "summary": "Fetch one resource and its current holders",
                    "operationId": "getResource",
                    "parameters": [
                        {
                            "name": "resource",
                            "in": "path",
                            "required": True,
                            "description": "Numeric id or exact name.",
                            "schema": {"type": "string"},
                            "example": "lab-pi-03",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "The resource, including who holds it.",
                            "content": {"application/json": {"schema": RESOURCE}},
                        },
                        "401": UNAUTHORIZED,
                        "404": {
                            "description": "No such resource.",
                            "content": {"application/json": {"schema": ERROR}},
                        },
                    },
                }
            },
            "/api/v1/locks": {
                "get": {
                    "tags": ["locks"],
                    "summary": "List the locks this token currently holds",
                    "operationId": "listLocks",
                    "responses": {
                        "200": {
                            "description": "Active locks held by the token holder.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "locks": {
                                                "type": "array",
                                                "items": LOCK,
                                            }
                                        },
                                    }
                                }
                            },
                        },
                        "401": UNAUTHORIZED,
                    },
                },
                "post": {
                    "tags": ["locks"],
                    "summary": "Lock a resource, by name/id or by label",
                    "description": (
                        "Give either `resource` (a specific machine) or `label` "
                        "(any free machine carrying that label), never both."
                    ),
                    "operationId": "createLock",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "resource": {
                                            "type": "string",
                                            "description": "Numeric id or exact name.",
                                        },
                                        "label": {
                                            "type": "string",
                                            "description": (
                                                "Lock the first free resource "
                                                "carrying this label."
                                            ),
                                        },
                                        "note": {
                                            "type": "string",
                                            "description": "What you are using it for.",
                                        },
                                    },
                                },
                                "examples": {
                                    "explicit": {
                                        "summary": "A specific device",
                                        "value": {
                                            "resource": "lab-pi-03",
                                            "note": "nightly firmware run",
                                        },
                                    },
                                    "byLabel": {
                                        "summary": "Any free device in rack B",
                                        "value": {
                                            "label": "rack-b",
                                            "note": "CI job 4711",
                                        },
                                    },
                                },
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "The slot is yours.",
                            "content": {"application/json": {"schema": LOCK}},
                        },
                        "400": {
                            "description": "Neither or both of resource/label given.",
                            "content": {"application/json": {"schema": ERROR}},
                        },
                        "401": UNAUTHORIZED,
                        "404": {
                            "description": "No such resource or label.",
                            "content": {"application/json": {"schema": ERROR}},
                        },
                        "409": {
                            "description": (
                                "Everything matching is occupied. Retry later."
                            ),
                            "content": {"application/json": {"schema": ERROR}},
                        },
                    },
                },
            },
            "/api/v1/locks/{lock_id}": {
                "delete": {
                    "tags": ["locks"],
                    "summary": "Release a lock",
                    "operationId": "deleteLock",
                    "parameters": [
                        {
                            "name": "lock_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Released.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "released": {"type": "integer"}
                                        },
                                    }
                                }
                            },
                        },
                        "401": UNAUTHORIZED,
                        "404": {
                            "description": "No such lock.",
                            "content": {"application/json": {"schema": ERROR}},
                        },
                        "409": {
                            "description": "Already released, or not yours.",
                            "content": {"application/json": {"schema": ERROR}},
                        },
                    },
                }
            },
        },
    }
