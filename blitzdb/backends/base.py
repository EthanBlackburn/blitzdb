import abc
import inspect
import copy

import logging

logger = logging.getLogger(__name__)

from blitzdb.document import Document, document_classes


class NotInTransaction(BaseException):
    """
    Gets raised if a function that must only be used inside a database transaction
    gets called outside a transaction.
    """


class InTransaction(BaseException):
    """
    Gets raised if a function that must only be used outside a database transaction
    gets called inside a transaction.
    """


class Backend(object):

    """
    Abstract base class for all backend implementations. Provides operations for querying the database,
    as well as for storing, updating and deleting documenta. 

    :param autodiscover_classes: If set to `True`, document classes will be discovered automatically,
                                 using a global list of all classes generated by the Document metaclass.
    
    *The `Meta` attribute*

    As with `blitzdb.document.Document`, the `Meta` attribute can be used to define certain class-wide
    settings and properties. Redefine it in your backend implementation to change the default values.

    """

    __metaclass__ = abc.ABCMeta

    def __init__(self, autodiscover_classes=True, autoload_embedded=True, allow_documents_in_query=True):
        self.classes = {}
        self.collections = {}
        self._autoload_embedded = autoload_embedded
        self._allow_documents_in_query = allow_documents_in_query
        if autodiscover_classes:
            self.autodiscover_classes()

    def autodiscover_classes(self):
        """
        Registers all document classes that have been defined in the code so far. The discovery mechanism
        works by reading the value of `blitzdb.document.document_classes`, which is updated by the meta-class
        of the :py:class:`blitzdb.document.Document` class upon creation of a new subclass.
        """
        for document_class in document_classes:
            self.register(document_class)

    def register(self, cls, parameters=None):
        """
        Explicitly register a new document class for use in the backend.

        :param cls:        A reference to the class to be defined
        :param parameters: A dictionary of parameters. Currently, only the `collection` parameter is used
                           to specify the collection in which to store the documents of the given class.

        .. admonition:: Registering classes

            If possible, always use `autodiscover_classes = True` or register your document classes beforehand
            using the `register` function, since this ensures that related documents can be initialized 
            appropriately. For example, suppose you have a document class `Author` that contains a list of 
            references to documents of class `Book`. If you retrieve an instance of an `Author` object from
            the database without having registered the `Book` class, the references to that class will not get
            parsed properly and will just show up as dictionaries containing a primary key and a collection name.

            Also, when :py:meth:`blitzdb.backends.base.Backend.autoregister` is used to register a class, 
            you can't pass in any parameters to customize e.g. the collection name for that class 
            (you can of course do this throught the `Meta` attribute of the class)
        """
        if parameters is None:
            parameters = {}
        if 'collection' in parameters:
            collection_name = parameters['collection']
        elif hasattr(cls.Meta,'collection'):
            collection_name = cls.Meta.collection
        else:
            collection_name = cls.__name__.lower()

        delete_list = []
        for new_cls, new_params in self.classes.items():
            if 'collection' in new_params and new_params['collection'] == collection_name:
                delete_list.append(new_cls)

        for delete_cls in delete_list:
            del self.classes[delete_cls]

        self.collections[collection_name] = cls
        self.classes[cls] = parameters.copy()
        self.classes[cls]['collection'] = collection_name

    def get_meta_attributes(self, cls):

        def get_user_attributes(cls):
            boring = dir(type('dummy', (object,), {}))
            return dict([item
                         for item in inspect.getmembers(cls)
                         if item[0] not in boring])
        
        if hasattr(cls, 'Meta'):
            params = get_user_attributes(cls.Meta)
        else:
            params = {}

        return params

    def autoregister(self, cls):
        """
        Autoregister a class that is encountered for the first time.

        :param cls: The class that should be registered.
        """

        params = self.get_meta_attributes(cls)
        return self.register(cls, params)

    def serialize(self, obj, convert_keys_to_str=False, embed_level=0, encoders=None, autosave=True, for_query=False):        
        """
        Serializes a given object, i.e. converts it to a representation that can be stored in the database.
        This usually involves replacing all `Document` instances by database references to them.

        :param obj: The object to serialize.
        :param convert_keys_to_str: If `True`, converts all dictionary keys to string (this is e.g. required for the MongoDB backend)
        :param embed_level: If `embed_level > 0`, instances of `Document` classes will be embedded instead of referenced. 
                            The value of the parameter will get decremented by 1 when calling `serialize` on child objects.
        :param autosave: Whether to automatically save embedded objects without a primary key to the database.
        :param for_query: If true, only the `pk` and `__collection__` attributes will be included in document references.

        :returns: The serialized object.
        """

        def get_value(obj,key):
            key_fragments = key.split(".")
            current_dict = obj
            for key_fragment in key_fragments:
                current_dict = current_dict[key_fragment]
            return current_dict

        serialize_with_opts = lambda value,*args,**kwargs : self.serialize(value,*args,convert_keys_to_str = convert_keys_to_str,autosave = autosave,for_query = for_query, **kwargs)
        if encoders:
            for matcher, encoder in encoders:
                if matcher(obj):
                    obj = encoder(obj)

        if isinstance(obj, dict):
            output_obj = {}
            for key, value in obj.items():
                output_obj[str(key) if convert_keys_to_str else key] = serialize_with_opts(value, embed_level=embed_level)
        elif isinstance(obj, list):
            output_obj = list(map(lambda x: serialize_with_opts(x, embed_level=embed_level), obj))
        elif isinstance(obj, tuple):
            output_obj = tuple(map(lambda x: serialize_with_opts(x, embed_level=embed_level), obj))
        elif isinstance(obj, Document):
            collection = self.get_collection_for_obj(obj)
            if embed_level > 0:
                try:
                    output_obj = serialize_with_opts(obj.eager.attributes, embed_level=embed_level - 1)
                except obj.DoesNotExist:#cannot load object, ignoring...
                    output_obj = serialize_with_opts(obj.attributes, embed_level=embed_level - 1)
            elif obj.embed:
                output_obj = obj.serialize(embed=True)
            else:
                if obj.pk == None and autosave:
                    obj.save(self)

                if obj._lazy:
                    # We make sure that all attributes that are already present get included in the reference
                    output_obj = copy.deepcopy(obj.lazy_attributes)
                    if obj.get_pk_name() in output_obj:
                        del output_obj[obj.get_pk_name()]
                    output_obj['pk'] = obj.pk
                    output_obj['__collection__'] = self.classes[obj.__class__]['collection']
                else:
                    if for_query and not self._allow_documents_in_query:
                        raise ValueError("Documents are not allowed in queries!")
                    output_obj = {'pk':obj.pk,'__collection__':self.classes[obj.__class__]['collection']}
                    #We include fields to the reference, as given by the document's Meta class
                    if hasattr(obj,'Meta') and hasattr(obj.Meta,'dbref_includes') and obj.Meta.dbref_includes:
                        for include_key in obj.Meta.dbref_includes:
                            try:
                                value = get_value(obj,include_key)
                                output_obj[include_key.replace(".","_")] = value
                            except KeyError:
                                continue
                

        else:
            output_obj = obj
        return output_obj

    def deserialize(self, obj, decoders=None):
        """
        Deserializes a given object, i.e. converts references to other (known) `Document` objects by lazy instances of the
        corresponding class. This allows the automatic fetching of related documents from the database as required.

        :param obj: The object to be deserialized.

        :returns: The deserialized object.
        """

        if decoders:
            for matcher, decoder in decoders:
                if matcher(obj):
                    obj = decoder(obj)
        if isinstance(obj, dict):
            if '__pk__' in obj:
                pk_field = '__pk__'
            elif 'pk' in obj:
                pk_field = 'pk'
            else:
                pk_field = None
            if '__collection__' in obj and obj['__collection__'] in self.collections and pk_field:
                #for backwards compatibility
                attributes = copy.deepcopy(obj)
                del attributes[pk_field]
                del attributes['__collection__']
                output_obj = self.create_instance(obj['__collection__'], attributes, lazy=True)
                output_obj.pk = obj[pk_field]
            else:
                output_obj = {}
                for (key, value) in obj.items():
                    output_obj[key] = self.deserialize(value)
        elif isinstance(obj, list) or isinstance(obj, tuple):
            output_obj = list(map(lambda x: self.deserialize(x), obj))
        else:
            output_obj = obj
        return output_obj

    def create_instance(self, collection_or_class, attributes, lazy=False):
        """
        Creates an instance of a `Document` class corresponding to the given collection name or class.

        :param collection_or_class: The name of the collection or a reference to the class for which to create an instance.
        :param attributes: The attributes of the instance to be created
        :param lazy: Whether to create a `lazy` object or not.

        :returns: An instance of the requested Document class with the given attributes.
        """
        if collection_or_class in self.classes:
            cls = collection_or_class
        elif collection_or_class in self.collections:
            cls = self.collections[collection_or_class]
        else:
            raise AttributeError("Unknown collection or class: %s!" % str(collection_or_class))

        if 'constructor' in self.classes[cls]:
            obj = self.classes[cls]['constructor'](attributes, lazy=lazy)
        else:
            obj = cls(attributes, lazy=lazy, default_backend=self, autoload=self._autoload_embedded)
        return obj

    def get_collection_for_obj(self, obj):
        """
        Returns the collection name for a given object, based on the class of the object.

        :param obj: The object for which to return the collection name.

        :returns: The collection name for the given object.
        """
        return self.get_collection_for_cls(obj.__class__)

    def get_collection_for_cls(self, cls): 
        """
        Returns the collection name for a given document class.

        :param cls: The document class for which to return the collection name.

        :returns: The collection name for the given class.
        """
        if cls not in self.classes:
            if issubclass(cls, Document) and cls not in self.classes:
                self.autoregister(cls)
            else:
                raise AttributeError("Unknown object type: %s" % cls.__name__)
        collection = self.classes[cls]['collection']
        return collection

    def get_cls_for_collection(self, collection):
        """
        Return the class for a given collection name.

        :param collection: The name of the collection for which to return the class.

        :returns: A reference to the class for the given collection name.
        """
        for cls, params in self.classes.items():
            if params['collection'] == collection:
                return cls
        raise AttributeError("Unknown collection: %s" % collection)

    @abc.abstractmethod
    def save(self, obj, cache=None):
        """
        Abstract method to save a `Document` instance to the database.

        :param obj: The object to be stored in the database.
        :param cache: Whether to performed a cached save operation (not supported by all backends).
        """

    @abc.abstractmethod
    def get(self, cls, properties):
        """
        Abstract method to retrieve a single object from the database according to a list of properties.

        :param cls: The class for which to return an object.
        :param properties: The properties of the object to be returned

        :returns: An instance of the requested object.

        .. admonition:: Exception Behavior

            Raises a :py:class:`blitzdb.document.Document.DoesNotExist` exception if no object with the given
            properties exists in the database, and a :py:class:`blitzdb.document.Document.MultipleObjectsReturned` 
            exception if more than one object in the database corresponds to the given properties.
        
        """

    @abc.abstractmethod
    def delete(self, obj):
        """
        Deletes an object from the database.

        :param obj: The object to be deleted.
        """

    @abc.abstractmethod
    def filter(self, cls, **kwargs):
        """
        Filter objects from the database that correspond to a given set of properties.

        :param cls: The class for which to filter objects from the database.
        :param properties: The properties used to filter objects.
        :param sorty_by: A field or list of fields according to which to sort the returned objects.
        :param limit: The maximal number of objects to return in a single query.
        :param offset: The offset in respect to the beginning of the result list (to be used in conjunction with `limit`).

        :returns: A `blitzdb.queryset.QuerySet` instance containing the keys of the objects matching the query.

        .. admonition:: Functionality might differ between backends
             
             Please be aware that the functionality of the `filter` function might
             differ from backend to backend. Consult the documentation of the given
             backend that you use to find out which queries are supported.


        """

