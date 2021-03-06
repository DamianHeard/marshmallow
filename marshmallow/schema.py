# -*- coding: utf-8 -*-
"""The :class:`Schema` class, including its metaclass and options (class Meta)."""
from __future__ import absolute_import, unicode_literals

from collections import defaultdict, Mapping
import copy
import datetime as dt
import decimal
import inspect
import json
import types
import uuid
import warnings
from collections import namedtuple
import functools

from marshmallow import base, fields, utils, class_registry, marshalling
from marshmallow.compat import (with_metaclass, iteritems, text_type,
                                binary_type, OrderedDict)
from marshmallow.exceptions import ValidationError
from marshmallow.orderedset import OrderedSet
from marshmallow.decorators import (PRE_DUMP, POST_DUMP, PRE_LOAD, POST_LOAD,
                                    VALIDATES, VALIDATES_SCHEMA)


#: Return type of :meth:`Schema.dump` including serialized data and errors
MarshalResult = namedtuple('MarshalResult', ['data', 'errors'])
#: Return type of :meth:`Schema.load`, including deserialized data and errors
UnmarshalResult = namedtuple('UnmarshalResult', ['data', 'errors'])

def _get_fields(attrs, field_class, pop=False, ordered=False):
    """Get fields from a class. If ordered=True, fields will sorted by creation index.

    :param attrs: Mapping of class attributes
    :param type field_class: Base field class
    :param bool pop: Remove matching fields
    """
    getter = getattr(attrs, 'pop' if pop else 'get')
    fields = [
        (field_name, getter(field_name))
        for field_name, field_value in list(iteritems(attrs))
        if utils.is_instance_or_subclass(field_value, field_class)
    ]
    if ordered:
        return sorted(
            fields,
            key=lambda pair: pair[1]._creation_index,
        )
    else:
        return fields

# This function allows Schemas to inherit from non-Schema classes and ensures
#   inheritance according to the MRO
def _get_fields_by_mro(klass, field_class, ordered=False):
    """Collect fields from a class, following its method resolution order. The
    class itself is excluded from the search; only its parents are checked. Get
    fields from ``_declared_fields`` if available, else use ``__dict__``.

    :param type klass: Class whose fields to retrieve
    :param type field_class: Base field class
    """
    mro = inspect.getmro(klass)
    # Loop over mro in reverse to maintain correct order of fields
    return sum(
        (
            _get_fields(
                getattr(base, '_declared_fields', base.__dict__),
                field_class,
                ordered=ordered
            )
            for base in mro[:0:-1]
        ),
        [],
    )


class SchemaMeta(type):
    """Metaclass for the Schema class. Binds the declared fields to
    a ``_declared_fields`` attribute, which is a dictionary mapping attribute
    names to field objects. Also sets the ``opts`` class attribute, which is
    the Schema class's ``class Meta`` options.
    """

    def __new__(mcs, name, bases, attrs):
        meta = attrs.get('Meta')
        ordered = getattr(meta, 'ordered', False)
        if not ordered:
            # Inherit 'ordered' option
            # Warning: We loop through bases instead of MRO because we don't
            # yet have access to the class object
            # (i.e. can't call super before we have fields)
            for base_ in bases:
                if hasattr(base_, 'Meta') and hasattr(base_.Meta, 'ordered'):
                    ordered = base_.Meta.ordered
                    break
            else:
                ordered = False
        cls_fields = _get_fields(attrs, base.FieldABC, pop=True, ordered=ordered)
        klass = super(SchemaMeta, mcs).__new__(mcs, name, bases, attrs)
        inherited_fields = _get_fields_by_mro(klass, base.FieldABC)

        # Use getattr rather than attrs['Meta'] so that we get inheritance for free
        meta = getattr(klass, 'Meta')
        # Set klass.opts in __new__ rather than __init__ so that it is accessible in
        # get_declared_fields
        klass.opts = klass.OPTIONS_CLASS(meta)
        # Add fields specifid in the `include` class Meta option
        cls_fields += list(klass.opts.include.items())

        dict_cls = OrderedDict if ordered else dict
        # Assign _declared_fields on class
        klass._declared_fields = mcs.get_declared_fields(
            klass=klass,
            cls_fields=cls_fields,
            inherited_fields=inherited_fields,
            dict_cls=dict_cls
        )
        return klass

    @classmethod
    def get_declared_fields(mcs, klass, cls_fields, inherited_fields, dict_cls):
        """Returns a dictionary of field_name => `Field` pairs declard on the class.
        This is exposed mainly so that plugins can add additional fields, e.g. fields
        computed from class Meta options.

        :param type klass: The class object.
        :param dict cls_fields: The fields declared on the class, including those added
            by the ``include`` class Meta option.
        :param dict inherited_fileds: Inherited fields.
        :param type dict_class: Either `dict` or `OrderedDict`, depending on the whether
            the user specified `ordered=True`.
        """
        return dict_cls(inherited_fields + cls_fields)

    # NOTE: self is the class object
    def __init__(self, name, bases, attrs):
        super(SchemaMeta, self).__init__(name, bases, attrs)
        class_registry.register(name, self)
        self._resolve_processors()

    def _resolve_processors(self):
        """Add in the decorated processors

        By doing this after constructing the class, we let standard inheritance
        do all the hard work.
        """
        mro = inspect.getmro(self)
        self.__processors__ = defaultdict(list)
        for attr_name in dir(self):
            # Need to look up the actual descriptor, not whatever might be
            # bound to the class. This needs to come from the __dict__ of the
            # declaring class.
            for parent in mro:
                try:
                    attr = parent.__dict__[attr_name]
                except KeyError:
                    continue
                else:
                    break
            else:
                # In case we didn't find the attribute and didn't break above.
                # We should never hit this - it's just here for completeness
                # to exclude the possibility of attr being undefined.
                continue

            try:
                processor_tags = attr.__marshmallow_tags__
            except AttributeError:
                continue

            for tag in processor_tags:
                # Use name here so we can get the bound method later, in case
                # the processor was a descriptor or something.
                self.__processors__[tag].append(attr_name)


class SchemaOpts(object):
    """class Meta options for the :class:`Schema`. Defines defaults."""

    def __init__(self, meta):
        self.fields = getattr(meta, 'fields', ())
        if not isinstance(self.fields, (list, tuple)):
            raise ValueError("`fields` option must be a list or tuple.")
        self.additional = getattr(meta, 'additional', ())
        if not isinstance(self.additional, (list, tuple)):
            raise ValueError("`additional` option must be a list or tuple.")
        if self.fields and self.additional:
            raise ValueError("Cannot set both `fields` and `additional` options"
                            " for the same Schema.")
        self.exclude = getattr(meta, 'exclude', ())
        if not isinstance(self.exclude, (list, tuple)):
            raise ValueError("`exclude` must be a list or tuple.")
        self.strict = getattr(meta, 'strict', False)
        self.dateformat = getattr(meta, 'dateformat', None)
        self.json_module = getattr(meta, 'json_module', json)
        if hasattr(meta, 'skip_missing'):
            warnings.warn(
                'The skip_missing option is no longer necessary. Missing inputs passed to '
                'Schema.dump will be excluded from the serialized output by default.',
                UserWarning
            )
        self.ordered = getattr(meta, 'ordered', False)
        self.index_errors = getattr(meta, 'index_errors', True)
        self.include = getattr(meta, 'include', {})
        self.load_only = getattr(meta, 'load_only', ())
        self.dump_only = getattr(meta, 'dump_only', ())


class BaseSchema(base.SchemaABC):
    """Base schema class with which to define custom schemas.

    Example usage:

    .. code-block:: python

        import datetime as dt
        from marshmallow import Schema, fields

        class Album(object):
            def __init__(self, title, release_date):
                self.title = title
                self.release_date = release_date

        class AlbumSchema(Schema):
            title = fields.Str()
            release_date = fields.Date()

        # Or, equivalently
        class AlbumSchema2(Schema):
            class Meta:
                fields = ("title", "release_date")

        album = Album("Beggars Banquet", dt.date(1968, 12, 6))
        schema = AlbumSchema()
        data, errors = schema.dump(album)
        data  # {'release_date': '1968-12-06', 'title': 'Beggars Banquet'}

    :param dict extra: A dict of extra attributes to bind to the serialized result.
    :param tuple only: A list or tuple of fields to serialize. If `None`, all
        fields will be serialized.
    :param tuple exclude: A list or tuple of fields to exclude from the
        serialized result.
    :param str prefix: Optional prefix that will be prepended to all the
        serialized field names.
    :param bool strict: If `True`, raise errors if invalid data are passed in
        instead of failing silently and storing the errors.
    :param bool many: Should be set to `True` if ``obj`` is a collection
        so that the object will be serialized to a list.
    :param dict context: Optional context passed to :class:`fields.Method` and
        :class:`fields.Function` fields.
    :param tuple load_only: A list or tuple of fields to skip during serialization
    :param tuple dump_only: A list or tuple of fields to skip during
        deserialization, read-only fields
    :param bool partial: If `True`, ignore missing fields when deserializing.

    .. versionchanged:: 2.0.0
        `__validators__`, `__preprocessors__`, and `__data_handlers__` are removed in favor of
        `marshmallow.decorators.validates_schema`,
        `marshmallow.decorators.pre_load` and `marshmallow.decorators.post_dump`.
        `__accessor__` and `__error_handler__` are deprecated. Implement the
        `handle_error` and `get_attribute` methods instead.
        """
    TYPE_MAPPING = {
        text_type: fields.String,
        binary_type: fields.String,
        dt.datetime: fields.DateTime,
        float: fields.Float,
        bool: fields.Boolean,
        tuple: fields.Raw,
        list: fields.Raw,
        set: fields.Raw,
        int: fields.Integer,
        uuid.UUID: fields.UUID,
        dt.time: fields.Time,
        dt.date: fields.Date,
        dt.timedelta: fields.TimeDelta,
        decimal.Decimal: fields.Decimal,
    }

    OPTIONS_CLASS = SchemaOpts

    #: DEPRECATED: Custom error handler function. May be `None`.
    __error_handler__ = None
    #: DEPRECATED: Function used to get values of an object.
    __accessor__ = None

    class Meta(object):
        """Options object for a Schema.

        Example usage: ::

            class Meta:
                fields = ("id", "email", "date_created")
                exclude = ("password", "secret_attribute")

        Available options:

        - ``fields``: Tuple or list of fields to include in the serialized result.
        - ``additional``: Tuple or list of fields to include *in addition* to the
            explicitly declared fields. ``additional`` and ``fields`` are
            mutually-exclusive options.
        - ``include``: Dictionary of additional fields to include in the schema. It is
            usually better to define fields as class variables, but you may need to
            use this option, e.g., if your fields are Python keywords. May be an
            `OrderedDict`.
        - ``exclude``: Tuple or list of fields to exclude in the serialized result.
        - ``dateformat``: Date format for all DateTime fields that do not have their
            date format explicitly specified.
        - ``strict``: If `True`, raise errors during marshalling rather than
            storing them.
        - ``json_module``: JSON module to use for `loads` and `dumps`.
            Defaults to the ``json`` module in the stdlib.
        - ``ordered``: If `True`, order serialization output according to the
            order in which fields were declared. Output of `Schema.dump` will be a
            `collections.OrderedDict`.
        - ``index_errors``: If `True`, errors dictionaries will include the index
            of invalid items in a collection.
        - ``load_only``: Tuple or list of fields to exclude from serialized results.
        - ``dump_only``: Tuple or list of fields to exclude from deserialization
        """
        pass

    def __init__(self, extra=None, only=(), exclude=(), prefix='', strict=False,
                 many=False, context=None, load_only=(), dump_only=(),
                 partial=False):
        # copy declared fields from metaclass
        self.declared_fields = copy.deepcopy(self._declared_fields)
        self.many = many
        self.only = only
        self.exclude = exclude
        self.prefix = prefix
        self.strict = strict or self.opts.strict
        self.ordered = self.opts.ordered
        self.load_only = set(load_only) or set(self.opts.load_only)
        self.dump_only = set(dump_only) or set(self.opts.dump_only)
        self.partial = partial
        #: Dictionary mapping field_names -> :class:`Field` objects
        self.fields = self.dict_class()
        #: Callable marshalling object
        self._marshal = marshalling.Marshaller(
            prefix=self.prefix
        )
        #: Callable unmarshalling object
        self._unmarshal = marshalling.Unmarshaller()
        if extra:
            warnings.warn(
                'The `extra` argument is deprecated. Use a post_dump '
                'method to add additional data instead.',
                DeprecationWarning
            )
        self.extra = extra
        self.context = context or {}
        self._update_fields(many=many)

    def __repr__(self):
        return '<{ClassName}(many={self.many}, strict={self.strict})>'.format(
            ClassName=self.__class__.__name__, self=self
        )

    def _postprocess(self, data, many, obj):
        if self.extra:
            if many:
                for each in data:
                    each.update(self.extra)
            else:
                data.update(self.extra)
        if self._marshal.errors:
            # TODO: Remove self.__error_handler__ in a later release
            error_handler = self.handle_error or self.__error_handler__
            if callable(error_handler):
                error_handler(self._marshal.errors, obj)

        return data

    @property
    def dict_class(self):
        return OrderedDict if self.ordered else dict

    @property
    def set_class(self):
        return OrderedSet if self.ordered else set

    ##### Override-able methods #####

    def handle_error(self, error, data):
        """Custom error handler function for the schema.

        :param ValidationError error: The `ValidationError` raised during (de)serialization.
        :param data: The original input data.

        .. versionadded:: 2.0.0
        """
        pass

    def get_attribute(self, attr, obj, default):
        """Defines how to pull values from an object to serialize.

        .. versionadded:: 2.0.0
        """
        return utils.get_value(attr, obj, default)

    ##### Handler decorators (deprecated) #####

    @classmethod
    def error_handler(cls, func):
        """Decorator that registers an error handler function for the schema.
        The function receives the :class:`Schema` instance, a dictionary of errors,
        and the serialized object (if serializing data) or data dictionary (if
        deserializing data) as arguments.

        Example: ::

            class UserSchema(Schema):
                email = fields.Email()

            @UserSchema.error_handler
            def handle_errors(schema, errors, obj):
                raise ValueError('An error occurred while marshalling {}'.format(obj))

            user = User(email='invalid')
            UserSchema().dump(user)  # => raises ValueError
            UserSchema().load({'email': 'bademail'})  # raises ValueError

        .. versionadded:: 0.7.0
        .. deprecated:: 2.0.0
            Set the ``error_handler`` class Meta option instead.
        """
        warnings.warn(
            'Schema.error_handler is deprecated. Set the error_handler class Meta option '
            'instead.', category=DeprecationWarning
        )
        cls.__error_handler__ = func
        return func

    @classmethod
    def accessor(cls, func):
        """Decorator that registers a function for pulling values from an object
        to serialize. The function receives the :class:`Schema` instance, the
        ``key`` of the value to get, the ``obj`` to serialize, and an optional
        ``default`` value.

        .. deprecated:: 2.0.0
            Set the ``error_handler`` class Meta option instead.
        """
        warnings.warn(
            'Schema.accessor is deprecated. Set the accessor class Meta option '
            'instead.', category=DeprecationWarning
        )
        cls.__accessor__ = func
        return func

    ##### Serialization/Deserialization API #####

    def dump(self, obj, many=None, update_fields=True, **kwargs):
        """Serialize an object to native Python data types according to this
        Schema's fields.

        :param obj: The object to serialize.
        :param bool many: Whether to serialize `obj` as a collection. If `None`, the value
            for `self.many` is used.
        :param bool update_fields: Whether to update the schema's field classes. Typically
            set to `True`, but may be `False` when serializing a homogenous collection.
            This parameter is used by `fields.Nested` to avoid multiple updates.
        :return: A tuple of the form (``data``, ``errors``)
        :rtype: `MarshalResult`, a `collections.namedtuple`

        .. versionadded:: 1.0.0
        """
        many = self.many if many is None else bool(many)
        if not many and utils.is_collection(obj) and not utils.is_keyed_tuple(obj):
            warnings.warn('Implicit collection handling is deprecated. Set '
                            'many=True to serialize a collection.',
                            category=DeprecationWarning)

        if isinstance(obj, types.GeneratorType):
            obj = list(obj)

        processed_obj = self._invoke_dump_processors(PRE_DUMP, obj, many, original_data=obj)

        if update_fields:
            self._update_fields(processed_obj, many=many)

        try:
            preresult = self._marshal(
                processed_obj,
                self.fields,
                many=many,
                # TODO: Remove self.__accessor__ in a later release
                accessor=self.get_attribute or self.__accessor__,
                dict_class=self.dict_class,
                index_errors=self.opts.index_errors,
                **kwargs
            )
        except ValidationError as error:
            errors = self._marshal.errors
            preresult = error.data
            if self.strict:
                raise error
        else:
            errors = {}
        result = self._postprocess(preresult, many, obj=obj)

        result = self._invoke_dump_processors(POST_DUMP, result, many, original_data=obj)

        return MarshalResult(result, errors)

    def dumps(self, obj, many=None, update_fields=True, *args, **kwargs):
        """Same as :meth:`dump`, except return a JSON-encoded string.

        :param obj: The object to serialize.
        :param bool many: Whether to serialize `obj` as a collection. If `None`, the value
            for `self.many` is used.
        :param bool update_fields: Whether to update the schema's field classes. Typically
            set to `True`, but may be `False` when serializing a homogenous collection.
            This parameter is used by `fields.Nested` to avoid multiple updates.
        :return: A tuple of the form (``data``, ``errors``)
        :rtype: `MarshalResult`, a `collections.namedtuple`

        .. versionadded:: 1.0.0
        """
        deserialized, errors = self.dump(obj, many=many, update_fields=update_fields)
        ret = self.opts.json_module.dumps(deserialized, *args, **kwargs)
        return MarshalResult(ret, errors)

    def load(self, data, many=None, partial=None):
        """Deserialize a data structure to an object defined by this Schema's
        fields and :meth:`make_object`.

        :param dict data: The data to deserialize.
        :param bool many: Whether to deserialize `data` as a collection. If `None`, the
            value for `self.many` is used.
        :param bool partial: Whether to ignore missing fields. If `None`, the
            value for `self.partial` is used.
        :return: A tuple of the form (``data``, ``errors``)
        :rtype: `UnmarshalResult`, a `collections.namedtuple`

        .. versionadded:: 1.0.0
        """
        result, errors = self._do_load(data, many, partial=partial, postprocess=True)
        return UnmarshalResult(data=result, errors=errors)

    def loads(self, json_data, many=None, *args, **kwargs):
        """Same as :meth:`load`, except it takes a JSON string as input.

        :param str json_data: A JSON string of the data to deserialize.
        :param bool many: Whether to deserialize `obj` as a collection. If `None`, the
            value for `self.many` is used.
        :param bool partial: Whether to ignore missing fields. If `None`, the
            value for `self.partial` is used.
        :return: A tuple of the form (``data``, ``errors``)
        :rtype: `UnmarshalResult`, a `collections.namedtuple`

        .. versionadded:: 1.0.0
        """
        # TODO: This avoids breaking backward compatibility if people were
        # passing in positional args after `many` for use by `json.loads`, but
        # ideally we shouldn't have to do this.
        partial = kwargs.pop('partial', None)

        data = self.opts.json_module.loads(json_data, *args, **kwargs)
        return self.load(data, many=many, partial=partial)

    def validate(self, data, many=None):
        """Validate `data` against the schema, returning a dictionary of
        validation errors.

        :param dict data: The data to validate.
        :param bool many: Whether to validate `data` as a collection. If `None`, the
            value for `self.many` is used.
        :return: A dictionary of validation errors.
        :rtype: dict

        .. versionadded:: 1.1.0
        """
        _, errors = self._do_load(data, many, postprocess=False)
        return errors

    ##### Private Helpers #####

    def _do_load(self, data, many=None, partial=None, postprocess=True):
        """Deserialize `data`, returning the deserialized result and a dictonary of
        validation errors.

        :param data: The data to deserialize.
        :param bool many: Whether to deserialize `data` as a collection. If `None`, the
            value for `self.many` is used.
        :param bool partial: Whether to ignore missing fields. If `None`, the
            value for `self.partial` is used.
        :param bool postprocess: Whether to run post_load methods..
        :return: A tuple of the form (`data`, `errors`)
        """
        many = self.many if many is None else bool(many)
        partial = self.partial if partial is None else bool(partial)

        processed_data = self._invoke_load_processors(PRE_LOAD, data, many, original_data=data)

        try:
            result = self._unmarshal(
                processed_data,
                self.fields,
                many=many,
                partial=partial,
                dict_class=self.dict_class,
                index_errors=self.opts.index_errors,
            )
        except ValidationError as error:
            result = error.data
        else:
            errors = {}
        self._invoke_field_validators(data=result, many=many)
        errors = self._unmarshal.errors
        # Run schema-level migration
        try:
            self._invoke_validators(pass_many=True, data=result, original_data=data, many=many)
        except ValidationError as err:
            errors.update(err.messages)
        try:
            self._invoke_validators(pass_many=False, data=result, original_data=data, many=many)
        except ValidationError as err:
            errors.update(err.messages)
        if errors:
            # TODO: Remove self.__error_handler__ in a later release
            if self.__error_handler__ and callable(self.__error_handler__):
                self.__error_handler__(errors, data)
            exc = ValidationError(
                errors,
                field_names=self._unmarshal.error_field_names,
                fields=self._unmarshal.error_fields,
                data=data
            )
            self.handle_error(exc, data)
            if self.strict:
                raise exc

        if not errors and postprocess:
            result = self._invoke_load_processors(POST_LOAD, result, many, original_data=data)

        return result, errors

    def _update_fields(self, obj=None, many=False):
        """Update fields based on the passed in object."""
        if self.only:
            # Return only fields specified in only option
            if self.opts.fields:
                field_names = self.set_class(self.opts.fields) & self.set_class(self.only)
            else:
                field_names = self.set_class(self.only)
        elif self.opts.fields:
            # Return fields specified in fields option
            field_names = self.set_class(self.opts.fields)
        elif self.opts.additional:
            # Return declared fields + additional fields
            field_names = (self.set_class(self.declared_fields.keys()) |
                            self.set_class(self.opts.additional))
        else:
            field_names = self.set_class(self.declared_fields.keys())

        # If "exclude" option or param is specified, remove those fields
        excludes = set(self.opts.exclude) | set(self.exclude)
        if excludes:
            field_names = field_names - excludes
        ret = self.__filter_fields(field_names, obj, many=many)
        # Set parents
        self.__set_field_attrs(ret)
        self.fields = ret
        return self.fields

    def on_bind_field(self, field_name, field_obj):
        """Hook to modify a field when it is bound to the `Schema`. No-op by default."""
        return None

    def __set_field_attrs(self, fields_dict):
        """Bind fields to the schema, setting any necessary attributes
        on the fields (e.g. parent and name).

        Also set field load_only and dump_only values if field_name was
        specified in ``class Meta``.
        """
        for field_name, field_obj in iteritems(fields_dict):
            try:
                if field_name in self.load_only:
                    field_obj.load_only = True
                if field_name in self.dump_only:
                    field_obj.dump_only = True
                self.on_bind_field(field_name, field_obj)
                field_obj._add_to_schema(field_name, self)
            except TypeError:
                # field declared as a class, not an instance
                if (isinstance(field_obj, type) and
                        issubclass(field_obj, base.FieldABC)):
                    msg = ('Field for "{0}" must be declared as a '
                           'Field instance, not a class. '
                           'Did you mean "fields.{1}()"?'
                           .format(field_name, field_obj.__name__))
                    raise TypeError(msg)
        return fields_dict

    def __filter_fields(self, field_names, obj, many=False):
        """Return only those field_name:field_obj pairs specified by
        ``field_names``.

        :param set field_names: Field names to include in the final
            return dictionary.
        :returns: An dict of field_name:field_obj pairs.
        """
        if obj and many:
            try:  # Homogeneous collection
                # Prefer getitem over iter to prevent breaking serialization
                # of objects for which iter will modify position in the collection
                # e.g. Pymongo cursors
                if hasattr(obj, '__getitem__') and callable(getattr(obj, '__getitem__')):
                    obj_prototype = obj[0]
                else:
                    obj_prototype = next(iter(obj))
            except (StopIteration, IndexError):  # Nothing to serialize
                return self.declared_fields
            obj = obj_prototype
        ret = self.dict_class()
        for key in field_names:
            if key in self.declared_fields:
                ret[key] = self.declared_fields[key]
            else:  # Implicit field creation (class Meta 'fields' or 'additional')
                if obj:
                    attribute_type = None
                    try:
                        if isinstance(obj, Mapping):
                            attribute_type = type(obj[key])
                        else:
                            attribute_type = type(getattr(obj, key))
                    except (AttributeError, KeyError) as err:
                        err_type = type(err)
                        raise err_type(
                            '"{0}" is not a valid field for {1}.'.format(key, obj))
                    field_obj = self.TYPE_MAPPING.get(attribute_type, fields.Field)()
                else:  # Object is None
                    field_obj = fields.Field()
                # map key -> field (default to Raw)
                ret[key] = field_obj
        return ret

    def _invoke_dump_processors(self, tag_name, data, many, original_data=None):
        # The pass_many post-dump processors may do things like add an envelope, so
        # invoke those after invoking the non-pass_many processors which will expect
        # to get a list of items.
        data = self._invoke_processors(tag_name, pass_many=False,
            data=data, many=many, original_data=original_data)
        data = self._invoke_processors(tag_name, pass_many=True,
            data=data, many=many, original_data=original_data)
        return data

    def _invoke_load_processors(self, tag_name, data, many, original_data=None):
        # This has to invert the order of the dump processors, so run the pass_many
        # processors first.
        data = self._invoke_processors(tag_name, pass_many=True,
            data=data, many=many, original_data=original_data)
        data = self._invoke_processors(tag_name, pass_many=False,
            data=data, many=many, original_data=original_data)
        return data

    def _invoke_field_validators(self, data, many):
        for attr_name in self.__processors__[(VALIDATES, False)]:
            validator = getattr(self, attr_name)
            validator_kwargs = validator.__marshmallow_kwargs__[(VALIDATES, False)]
            field_name = validator_kwargs['field_name']

            try:
                field_obj = self.fields[field_name]
            except KeyError:
                if field_name in self.declared_fields:
                    continue
                raise ValueError('"{0}" field does not exist.'.format(field_name))

            if many:
                for idx, item in enumerate(data):
                    try:
                        value = item[field_name]
                    except KeyError:
                        pass
                    else:
                        self._unmarshal.call_and_store(
                            getter_func=validator,
                            data=value,
                            field_name=field_name,
                            field_obj=field_obj,
                            index=(idx if self.opts.index_errors else None)
                        )
            else:
                try:
                    value = data[field_name]
                except KeyError:
                    pass
                else:
                    self._unmarshal.call_and_store(
                        getter_func=validator,
                        data=value,
                        field_name=field_name,
                        field_obj=field_obj
                    )

    def _invoke_validators(self, pass_many, data, original_data, many):
        errors = {}
        for attr_name in self.__processors__[(VALIDATES_SCHEMA, pass_many)]:
            validator = getattr(self, attr_name)
            validator_kwargs = validator.__marshmallow_kwargs__[(VALIDATES_SCHEMA, pass_many)]
            pass_original = validator_kwargs.get('pass_original', False)
            if pass_many:
                validator = functools.partial(validator, many=many)
            if many and not pass_many:
                for idx, item in enumerate(data):
                    try:
                        self._unmarshal.run_validator(validator,
                                                  item, original_data, self.fields, many=many,
                                                  index=idx, pass_original=pass_original)
                    except ValidationError as err:
                        errors.update(err.messages)
            else:
                try:
                    self._unmarshal.run_validator(validator,
                                              data, original_data, self.fields, many=many,
                                              pass_original=pass_original)
                except ValidationError as err:
                    errors.update(err.messages)
        if errors:
            raise ValidationError(errors)
        return None

    def _invoke_processors(self, tag_name, pass_many, data, many, original_data=None):
        for attr_name in self.__processors__[(tag_name, pass_many)]:
            # This will be a bound method.
            processor = getattr(self, attr_name)

            processor_kwargs = processor.__marshmallow_kwargs__[(tag_name, pass_many)]
            pass_original = processor_kwargs.get('pass_original', False)

            if pass_many:
                if pass_original:
                    data = utils.if_none(processor(data, many, original_data), data)
                else:
                    data = utils.if_none(processor(data, many), data)
            elif many:
                if pass_original:
                    data = [utils.if_none(processor(item, original_data), item)
                            for item in data]
                else:
                    data = [utils.if_none(processor(item), item) for item in data]
            else:
                if pass_original:
                    data = utils.if_none(processor(data, original_data), data)
                else:
                    data = utils.if_none(processor(data), data)
        return data


class Schema(with_metaclass(SchemaMeta, BaseSchema)):
    __doc__ = BaseSchema.__doc__
