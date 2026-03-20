# Lombok Reference

Patterns for using Lombok with functional Java code.

## @Value for Immutable Objects (CRITICAL)

```java
// GOOD: @Value creates immutable class with all-args constructor
@Value
public class UserRequest {
    String userId;
    String query;
    Instant timestamp;
}

// BAD: @Data is mutable, @Builder allows partial states
@Data
@Builder
public class UserRequest {  // AVOID
    private String userId;
    private String query;
}
```

## @With for Immutable Mutations — Avoid on Classes with Boxed Fields

**CRITICAL**: `@With` generates `this.field == newValue ? this : new Obj(...)` for EVERY field. The `==` comparison is a SpotBugs `RC_REF_COMPARISON` bug for boxed types (`Integer`, `Long`, `Boolean`).

**Rule**: Do NOT add class-level `@With` to any class that has boxed primitive fields. Use `toBuilder()` instead.

```java
// BAD: @With on a class with boxed fields
@Value
@Builder(toBuilder = true)
@With  // AVOID — SpotBugs RC_REF_COMPARISON on Integer/Long/Boolean fields
public class QueryRequest {
    String query;
    Integer limit;     // == unreliable for boxed Integer
    Long variantId;    // == unreliable for boxed Long
}

// GOOD: Use toBuilder() for multi-field immutable updates
QueryRequest updated = request.toBuilder()
    .query(newQuery)
    .limit(20)
    .build();

// GOOD: @With is safe when the class has ONLY reference types (String, domain objects)
@Value
@With
public class QueryContext {
    String query;
    List<String> sources;
}
```

## @FieldDefaults for Non-@Value Classes

`@Value` already includes `private final` for all fields. Use `@FieldDefaults` only for classes that are not `@Value`.

```java
// @Value classes - @FieldDefaults is REDUNDANT
@Value
public class ArticleData {
    String title;      // Already private final via @Value
    String content;
}

// Non-@Value classes - use @FieldDefaults
@FieldDefaults(level = AccessLevel.PRIVATE, makeFinal = true)
@RequiredArgsConstructor
public class ArticleProcessor {
    ArticleRepository repository;  // private final via @FieldDefaults
    MetricsService metrics;
}
```

## Factory Methods Over Constructors

```java
@Value
public class PublisherId {
    int value;

    public static Option<PublisherId> of(Integer raw) {
        return Option.of(raw)
            .filter(v -> v > 0)
            .map(PublisherId::new);
    }

    private PublisherId(int value) {
        this.value = value;
    }
}
```

## @Jacksonized Only for External JSON Boundaries

Use `@Jacksonized` with `@Builder` **only** at system boundaries where external JSON is parsed. After parsing, convert to domain objects that use plain `@Value`.

```java
// External DTO at API boundary
@Value
@Builder
@Jacksonized
public class ExternalApiResponseDto {
    String status;
    List<RawItem> items;

    public ApiResponse toDomain() {
        return new ApiResponse(
            Status.fromString(status),
            items.stream().map(RawItem::toDomain).collect(toList())
        );
    }
}

// Domain object - plain @Value, no builder
@Value
public class ApiResponse {
    Status status;
    List<Item> items;
}
```

## CRITICAL: Never Use Lombok with Class Inheritance

Lombok (`@SuperBuilder`, `@Value`, `@Builder`, `@With`) has **severe bugs** with class inheritance.

### Problem: @SuperBuilder toBuilder() Bug

`toBuilder().build()` **does NOT copy parent class fields** — they become `null`.

```java
// BROKEN: @SuperBuilder with inheritance
@SuperBuilder(toBuilder = true)
@Getter
public class BaseRequest {
    String publisher;
    String itemId;
}

@SuperBuilder(toBuilder = true)
@Getter
public class UserRequest extends BaseRequest {
    String userId;
}

// BUG: After toBuilder(), publisher and itemId are NULL!
UserRequest copy = original.toBuilder().build();
```

### Solution: Use Interfaces Instead of Inheritance

```java
public interface HasSourceItem {
    String getPublisher();
    String getItemId();
}

@Value
@Builder(toBuilder = true)
public class UserRequest implements HasSourceItem {
    String publisher;  // Declare fields directly
    String itemId;
    String userId;
}
```

## Avoid @Builder and @NoArgsConstructor

```java
// BAD: Builder allows partial state
@Builder
public class Request {
    String required1;
    String required2;
}
// Risk: Request.builder().required1("x").build() - missing required2!

// BAD: NoArgsConstructor enables mutable patterns
@NoArgsConstructor
public class Response { }  // AVOID
```
