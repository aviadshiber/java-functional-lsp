# Spring Framework Reference

Spring configuration patterns for functional Java.

## Bean Configuration (CRITICAL)

Always define beans in `@Configuration` classes. Never use `@Component`, `@Service`, or `@Repository`.

```java
// GOOD: Explicit bean configuration
@Configuration
public class SearchConfiguration {
    @Bean
    public SearchService searchService(VespaClient client, Repository repo) {
        return new SearchServiceImpl(client, repo);
    }
}

// BAD: Stereotype annotations
@Service  // NEVER use
public class SearchServiceImpl implements SearchService { }

@Component  // NEVER use
public class VespaClient { }
```

## Constructor Injection Only

```java
// GOOD: Constructor injection (via @Value)
@Value
public class QueryProcessor {
    SearchService searchService;
    MetricsService metrics;
}

// BAD: Field injection
public class QueryProcessor {
    @Autowired  // NEVER do this
    private SearchService searchService;
}
```

## Service Implementation Pattern

Combine `@Value` with `@Configuration` for clean, immutable services:

```java
// Service implementation - NO @Service annotation
@Value
public class SearchServiceImpl implements SearchService {
    VespaClient vespaClient;
    CacheManager cache;

    @Override
    public Either<SearchError, SearchResults> search(SearchQuery query) {
        return vespaClient.query(query);
    }
}

// Configuration class wires it together
@Configuration
public class SearchConfiguration {
    @Bean
    public SearchService searchService(VespaClient client, CacheManager cache) {
        return new SearchServiceImpl(client, cache);
    }
}
```

## Boundary Validation Principle

Validate inputs at **system boundaries** (controllers, consumers), not deep inside services/DAOs.

```java
// GOOD: Validate at the endpoint
@GetMapping("/items")
public Response getItems(@RequestParam Long itemId) {
    if (itemId == null) {
        return Response.badRequest("itemId is required");
    }
    return service.findById(itemId)
        .map(Response::ok)
        .getOrElse(Response::notFound);
}

// GOOD: Internal method trusts its callers
public Option<Item> findById(Long itemId) {
    return Option.of(repository.findById(itemId));
}

// BAD: Defensive null check deep in DAO — masks caller bugs
public Option<Item> findById(Long itemId) {
    if (itemId == null) return Option.none();  // Silently hides bugs
    // ...
}
```
